"""Entry point: python -m src.run

Each run:
  1. The Muse sweep (about daily): entry-level jobs in every criteria city
  2. Auto-discovery: companies found there get their own board added if it exists
  3. Poll every company board (watchlist.yaml + discovered) — hourly
  4. Must-have rules → SQLite (data/pipeline.db) → Google Sheet
The same job from a search source and a company board is kept once (board wins).
No scoring: the owner decides each posting in the Sheet.

  python -m src.run                                  # full run
  python -m src.run --print --company "<name>"       # console report for one watchlist board
  python -m src.run --print --company "<name>" --fixture <saved API response>
  python -m src.run --fake-sheet data/fake_sheet.json   # write to a local stand-in Sheet
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import logging
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .config import CONFIG_DIR, Company, load_criteria, load_watchlist, load_yaml
from .output.console import report
from .output.sheet import backend_from_env, sync_all
from .store.db import DEFAULT_DB, Store, now_iso
from .pipeline.gates import Evaluation, Gates
from .pipeline.locations import LocationMatcher
from .pipeline.normalize import normalize_posting
from .resolve.discover import board_from_url, find_board, verify_board
from .sources import SourceError, get_source, load_fixture
from .sources.jsearch import JSearchSource, search as jsearch_search
from .sources.muse import MuseSource, muse_locations
from .textmatch import normalize

log = logging.getLogger("pipeline")


DETAIL_PLATFORMS = ("smartrecruiters", "workday")
PARALLEL_BOARDS = 8


def evaluate_company(company, payload=None, criteria=None, gates=None, matcher=None,
                     store: Store | None = None, ts: str | None = None,
                     cache: dict | None = None) -> list[Evaluation]:
    """Fetch + normalize + rules for one board. Thread-safe when `store` is None
    (pass a preloaded detail `cache` instead)."""
    criteria = criteria or load_criteria()
    matcher = matcher or LocationMatcher(criteria)
    gates = gates or Gates(criteria)
    src = get_source(company.platform)
    if payload is not None:
        raws = src.parse(payload, company.slug)
    elif company.platform in DETAIL_PLATFORMS:
        raws = _fetch_with_details(src, company, criteria, gates, matcher, store, ts, cache)
    else:
        raws = src.fetch(company.slug)
    return [evaluate_one(r, company, criteria, gates, matcher) for r in raws]


def evaluate_one(raw, company, criteria, gates, matcher) -> Evaluation:
    """Rules for one posting. Fast path: a posting that already fails on its title,
    location or job type is not put through the (slow) description parsing."""
    stub = dataclasses.replace(raw, description_lines=[])
    quick = gates.evaluate(normalize_posting(stub, company, criteria, matcher))
    if not quick.passed:
        quick.posting.raw = raw            # keep the full posting for the snapshot
        quick.posting.description_lines = raw.description_lines
        return quick
    return gates.evaluate(normalize_posting(raw, company, criteria, matcher))


def _fetch_with_details(src, company, criteria, gates, matcher, store, ts, cache=None):
    """SmartRecruiters / Workday: list every poll; fetch descriptions only for postings
    that could pass on title + location (cached, so each is fetched once)."""
    from .sources.base import RawPosting

    def want(item: dict) -> bool:
        if src.platform == "workday":
            stub = RawPosting(platform=src.platform, slug=company.slug, native_id="stub",
                              title=item.get("title") or "", url="",
                              locations=[item.get("locationsText") or ""])
            if "location" in (item.get("locationsText") or "").lower():   # "2 Locations": decide after detail
                stub.locations = ["New York, NY"]
        else:
            loc = (item.get("location") or {})
            stub = RawPosting(platform=src.platform, slug=company.slug, native_id=str(item["id"]),
                              title=item.get("name") or "", url="",
                              locations=[loc["fullLocation"]] if loc.get("fullLocation") else [],
                              country=(loc.get("country") or "").upper() or None,
                              workplace_type="remote" if loc.get("remote") and not loc.get("hybrid") else None,
                              employment_type=(item.get("typeOfEmployment") or {}).get("label"))
        return gates.evaluate(normalize_posting(stub, company, criteria, matcher)).passed

    prefix = f"{src.platform}:{company.slug}:"
    if cache is not None:                      # caller loads and saves the cache (parallel polling)
        return src.fetch(company.slug, want=want, cache=cache)
    cache = store.details_for(prefix) if store is not None else {}
    before = set(cache)
    raws = src.fetch(company.slug, want=want, cache=cache)
    if store is not None:
        store.save_details(prefix, {k: v for k, v in cache.items() if k not in before}, ts or now_iso())
    return raws


SOURCE_NAMES = {"lever": "Lever", "greenhouse": "Greenhouse", "ashby": "Ashby", "muse": "The Muse",
                "smartrecruiters": "SmartRecruiters", "jsearch": "JSearch"}
SEARCH_PLATFORMS = ("muse", "jsearch")


def evaluate_raws(raws, criteria, gates, matcher, company: Company | None = None) -> list[Evaluation]:
    out = []
    for r in raws:
        comp = company or Company(name=r.company or r.slug, platform=r.platform, slug=r.slug)
        out.append(evaluate_one(r, comp, criteria, gates, matcher))
    return out


def dedupe_key(ev: Evaluation) -> str:
    """Same company + title + city = same job, whichever source it came from."""
    p = ev.posting
    company = re.sub(r"[^a-z0-9]", "", normalize(p.company))
    title = " ".join(re.findall(r"[a-z0-9]+", normalize(p.title)))
    city = p.location.best.city.lower() if p.location.best else ""
    return f"{company}|{title}|{city}"


def display_fields(ev: Evaluation) -> dict:
    """Facts shown in the Sheet."""
    p = ev.posting
    b = p.location.best
    where = f"{b.city} (tier {b.tier})" if b else "; ".join(p.locations)
    extra = ", ".join(t for t in p.comp.extra_terms if "structured" not in t) or p.comp.extra_comp_display
    return {"field": ev.field, "visa": ev.visa, "location": f"{where} · {p.work_arrangement}",
            "tier": b.tier if b else 9, "salary": p.comp.display(), "extra_comp": extra if extra != "—" else "",
            "experience": p.experience.label(),
            "source": SOURCE_NAMES.get(p.raw.platform, p.raw.platform) if p.raw else ""}


def store_evaluations(store: Store, company, evals: list[Evaluation], ts: str, criteria=None,
                      prefix: str | None = None, board_keys: set[str] | None = None,
                      board_companies: set[str] | None = None) -> dict:
    """Save one board's (or one search sweep's) postings and close the ones that vanished.

    Search-source postings are skipped when the same job is already on a company
    board; a company-board posting absorbs any earlier search copy (your
    decisions carry over).
    """
    counts = {"seen": len(evals), "new": 0, "passed": 0, "new_passed": 0, "reasons": Counter()}
    is_search = company.platform in SEARCH_PLATFORMS
    if not is_search:
        store.upsert_company(company.name, company.platform, company.slug)
    seen = []
    batch_keys: dict[str, str] = {}
    for ev in evals:
        p = ev.posting
        key = dedupe_key(ev)
        if not is_search and ev.passed:
            if key in batch_keys:
                counts["reasons"]["duplicate (same board, same title + city)"] += 1
                seen.append(p.posting_id)     # still open on the board — just not shown twice
                store.merge_into(p.posting_id, batch_keys[key])
                continue
            batch_keys[key] = p.posting_id
        if is_search and ((board_keys and key in board_keys) or store.open_board_key(key)):
            counts["reasons"]["duplicate (on company board)"] += 1
            continue        # the company-board copy is stored instead
        if is_search and board_companies and normalize(p.company) in board_companies:
            counts["reasons"]["stale (not on company board)"] += 1
            continue        # we watch this company's board and the job isn't on it: not active
        if is_search and store.open_key(key, f"{company.platform}:"):
            counts["reasons"]["duplicate (other search source)"] += 1
            continue        # the same job is already in from another search source
        # a full copy is kept only for postings that pass (the ones you may apply to)
        snapshot = (p.raw.raw if p.raw else {}) if ev.passed else None
        info = store.upsert_posting(p.posting_id, p.company, p.title, p.url, p.posted_at,
                                    snapshot, ts, dedupe_key=key)
        if not is_search:
            store.merge_search_copies(p.posting_id, key, ts)
        seen.append(p.posting_id)
        p.first_seen = info["first_seen"]
        counts["new"] += int(info["is_new"])
        counts["passed"] += int(ev.passed)
        counts["new_passed"] += int(ev.passed and info["is_new"])
        if not ev.passed:
            counts["reasons"][ev.failures[0].gate] += 1
        store.save_score(p.posting_id, ev.passed,
                         [{"gate": f.gate, "matched": f.matched} for f in ev.failures],
                         ev.flags, display_fields(ev), ts)
    store.close_missing(prefix or f"{company.platform}:{company.slug}:", seen, ts)
    return counts


def add_stats(stats: dict, source: str, c: dict) -> None:
    s = stats.setdefault(source, {"seen": 0, "new": 0, "passed": 0, "new_passed": 0, "reasons": Counter()})
    for k in ("seen", "new", "passed", "new_passed"):
        s[k] += c[k]
    s["reasons"].update(c["reasons"])


def load_sources_config() -> dict:
    path = CONFIG_DIR / "sources.yaml"
    return load_yaml(path) if path.exists() else {}


def run_muse(store: Store, cfg: dict, criteria, gates, matcher, ts: str, payload=None):
    """Daily Muse sweep. Returns (evals, complete) or (None, False) when not due."""
    if not cfg.get("enabled") or (payload is None and not store.source_due("muse", cfg.get("every_hours", 20), ts)):
        return None, False
    src = MuseSource(cfg)
    if payload is None:
        try:
            payload, complete = src.sweep(muse_locations(criteria))
        except SourceError as exc:
            log.error("The Muse: %s", exc)
            store.board_error("The Muse (search)", "muse", str(exc), ts)
            return None, False
    else:
        complete = True
    raws = src.parse(payload)
    log.info("The Muse: %d recent entry-level postings in your cities", len(raws))
    return evaluate_raws(raws, criteria, gates, matcher), complete


def run_jsearch(store: Store, cfg: dict, criteria, gates, matcher, ts: str) -> list[Evaluation] | None:
    """Spend today's JSearch budget on the least-recently searched keyword x city pairs."""
    if not cfg.get("enabled"):
        return None
    if not os.environ.get("JSEARCH_API_KEY"):
        log.info("JSearch: skipped (no JSEARCH_API_KEY)")
        return None
    budget = store.budget_left("jsearch", int(cfg.get("requests_per_day", 0)), ts)
    if budget <= 0:
        return None
    pairs = {f"{kw} in {c['query']}": c for kw in cfg.get("keywords", []) for c in cfg.get("cities", [])}
    src, evals, spent = JSearchSource(), [], 0
    sess = requests.Session()
    for pair, first in store.next_pairs("jsearch", list(pairs), budget):
        window = cfg.get("first_fill_window", "month") if first else cfg.get("window", "3days")
        try:
            payload = jsearch_search(pair, pairs[pair].get("country", "us"), window, sess)
        except SourceError as exc:
            log.error("JSearch: %s", exc)
            store.board_error("JSearch (search)", "jsearch", str(exc), ts)
            break
        spent += 1
        raws = src.parse(payload)
        store.mark_pair("jsearch", pair, len(raws), ts)
        evals += evaluate_raws(raws, criteria, gates, matcher)
    store.spend("jsearch", spent, ts)
    store.commit()
    log.info("JSearch: %d searches, %d postings", spent, len(evals))
    return evals


def discover_from_hn(store: Store, cfg: dict, known: set[str], matcher, ts: str) -> list[Company]:
    """Board links in Hacker News 'Who is hiring?' posts that mention one of your cities."""
    from .sources import hn
    sess = requests.Session()
    names = sorted({n for n, *_ in matcher.names}, key=len, reverse=True)
    city_rx = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b", re.I)
    candidates: dict[tuple[str, str], str] = {}
    for item_id in hn.recent_threads(int(cfg.get("threads", 3)), sess):
        for post in hn.posts(hn.thread(item_id, sess)):
            if not city_rx.search(post["text"]):
                continue
            for link in post["links"]:
                b = board_from_url(link)
                if not b:
                    continue
                name = post["company"]
                if len(name.split()) > 5 or city_rx.fullmatch(name.strip()) or not name[:1].isupper():
                    # free-form first line: name the company after its board instead
                    slug = b[1].split("/")[-1] if b[0] == "workday" else b[1]
                    name = re.sub(r"[-_.]+", " ", slug).strip().title()
                if normalize(name) not in known:
                    candidates.setdefault(b, name)
    todo = [(b, c) for b, c in candidates.items() if store.probe_due(f"hn:{b[0]}:{b[1]}", 30, ts)]
    with ThreadPoolExecutor(max_workers=PARALLEL_BOARDS) as pool:
        counts = list(pool.map(lambda bc: verify_board(bc[0][0], bc[0][1], requests.Session()), todo))
    found = []
    for ((platform, slug), company), n in zip(todo, counts):
        store.record_probe(f"hn:{platform}:{slug}", f"{n} postings", ts)
        if n and normalize(company) not in known:
            store.add_discovered(company, platform, slug, "Hacker News", "", ts)
            known.add(normalize(company))
            found.append(Company(name=company, platform=platform, slug=slug))
            log.info("Discovered %s board for %s from Hacker News (%d postings)", platform, company, n)
    store.commit()
    log.info("Hacker News: %d board links in posts about your cities, %d new boards", len(candidates), len(found))
    return found


def discover(store: Store, evals: list[Evaluation], cfg: dict, known: set[str], ts: str) -> list[Company]:
    """Probe for the company boards behind search results. Returns newly found boards."""
    if not cfg.get("enabled", True):
        return []
    by_company: dict[str, dict] = {}
    for ev in evals:
        if not ev.posting.location.offices:
            continue   # only companies hiring in your cities
        p = ev.posting
        hint = p.raw.slug if p.raw and p.raw.platform == "muse" else None
        site = (p.raw.raw.get("website") if p.raw else None) or ""
        m = re.search(r"https?://(?:www\.)?([a-z0-9-]+)\.", site.lower())
        entry = by_company.setdefault(p.company, {"titles": [], "hint": hint or (m.group(1) if m else None)})
        entry["titles"].append(p.title)
    found = []
    sess = requests.Session()
    # 1. apply links that point straight into a company board — no guessing needed
    for ev in evals:
        p = ev.posting
        if normalize(p.company) in known or not p.location.offices:
            continue
        for link in (p.raw.raw.get("apply_links") if p.raw else None) or []:
            hit = board_from_url(link or "")
            if not hit:
                dm = re.match(r"https?://(?:www\.)?([^/]+)", link or "")
                if dm:
                    store.count_link_domain(dm.group(1).lower())
                continue
            platform, slug = hit
            if platform == "workday":
                store.record_ats_link(slug, "workday", p.company, ts)
            n = verify_board(platform, slug, sess)
            if n:
                store.add_discovered(p.company, platform, slug, SOURCE_NAMES.get(p.raw.platform, ""), p.title, ts)
                store.record_probe(p.company, f"{platform}:{slug}", ts)
                known.add(normalize(p.company))
                found.append(Company(name=p.company, platform=platform, slug=slug))
                log.info("Discovered %s board for %s from an apply link (%d postings)", platform, p.company, n)
                break
    # 2. otherwise try likely board names (several companies at once)
    todo = []
    for name, info in sorted(by_company.items()):
        if normalize(name) in known or not store.probe_due(name, int(cfg.get("retry_unfound_after_days", 30)), ts):
            continue
        if len(todo) >= int(cfg.get("max_new_companies_per_run", 40)):
            break
        todo.append((name, info))

    def _probe(item):
        name, info = item
        try:
            return find_board(name, info["titles"], info["hint"], requests.Session())
        except Exception as exc:
            log.warning("discovery: %s failed (%s)", name, exc)
            return None
    with ThreadPoolExecutor(max_workers=PARALLEL_BOARDS) as pool:
        hits = list(pool.map(_probe, todo))
    for (name, info), hit in zip(todo, hits):
        store.record_probe(name, f"{hit.platform}:{hit.slug}" if hit else "none", ts)
        if hit:
            store.add_discovered(name, hit.platform, hit.slug, "search", hit.matched_title, ts)
            found.append(Company(name=name, platform=hit.platform, slug=hit.slug))
            log.info("Discovered %s board for %s (%s, %d postings)", hit.platform, name, hit.slug, hit.postings)
    store.commit()
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Job pipeline")
    ap.add_argument("--print", action="store_true", help="print a console report (step 1)")
    ap.add_argument("--company", help="only this watchlist company")
    ap.add_argument("--fixture", help="use a saved API response instead of the live board")
    ap.add_argument("--brief", action="store_true", help="omit the dropped list")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path")
    ap.add_argument("--fake-sheet", help="write the Sheet to this local JSON file instead of Google")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    criteria = load_criteria()
    matcher = LocationMatcher(criteria)
    gates = Gates(criteria)
    companies = load_watchlist()
    single = bool(args.company or args.fixture)
    if args.company:
        companies = [c for c in companies if c.name.lower() == args.company.lower()]
        if not companies:
            log.error("company %s not in config/watchlist.yaml", args.company)
            return 2
    ts = now_iso()
    store = None if args.print else Store(args.db)
    totals = {"boards_polled": 0, "postings_seen": 0, "new": 0, "passed": 0, "errors": 0}
    stats: dict[str, dict] = {}
    t0 = time.time()

    # 1–2. search sources + discovery (full runs only)
    muse_evals, muse_complete, jsearch_evals = (None, False, None)
    if store is not None and not single:
        cfg = load_sources_config()
        muse_evals, muse_complete = run_muse(store, cfg.get("muse") or {}, criteria, gates, matcher, ts)
        jsearch_evals = run_jsearch(store, cfg.get("jsearch") or {}, criteria, gates, matcher, ts)
        known = {normalize(c.name) for c in companies} | {normalize(r["company"]) for r in store.discovered_boards()}
        if muse_evals or jsearch_evals:
            try:
                discover(store, (muse_evals or []) + (jsearch_evals or []), cfg.get("discovery") or {}, known, ts)
            except Exception as exc:  # discovery problems never stop the run
                log.error("discovery error: %s", exc)
                totals["errors"] += 1
        hn_cfg = cfg.get("hackernews") or {}
        if hn_cfg.get("enabled") and store.source_due("hackernews", hn_cfg.get("every_hours", 24), ts):
            try:
                discover_from_hn(store, hn_cfg, known, matcher, ts)
                store.source_complete("hackernews", ts)
            except Exception as exc:  # never stops the run
                log.error("Hacker News discovery error: %s", exc)
        watch = {normalize(c.name) for c in companies}
        companies += [Company(name=r["company"], platform=r["platform"], slug=r["slug"])
                      for r in store.discovered_boards() if normalize(r["company"]) not in watch]

    # 3. company boards
    board_keys: set[str] = set()
    board_companies: set[str] = set()
    boards_cfg = load_sources_config().get("boards") or {}
    every = boards_cfg.get("every_hours") or {}
    # a) decide what to poll and load detail caches (database work stays on this thread)
    jobs = []
    for company in companies:
        payload = load_fixture(args.fixture) if args.fixture else None
        hours = every.get(company.platform)
        if store is not None and payload is None and store.board_is_quiet(
                company.name, boards_cfg.get("quiet_after_days", 3), ts):
            hours = max(hours or 0, boards_cfg.get("quiet_every_hours", 6))
        if store is not None and payload is None and hours and not store.source_due(f"board:{company.platform}:{company.slug}", hours, ts):
            continue        # polled less often (large or quiet board); its postings stay as they were
        cache = None
        if store is not None and payload is None and company.platform in DETAIL_PLATFORMS:
            cache = store.details_for(f"{company.platform}:{company.slug}:")
        jobs.append((company, payload, hours, cache, set(cache or {})))

    # b) fetch + rules in parallel (network-bound)
    def _poll(job):
        company, payload, _, cache, _ = job
        try:
            return evaluate_company(company, payload, criteria, gates, matcher, None, ts, cache), None
        except Exception as exc:      # one bad board never stops the run
            return None, exc
    with ThreadPoolExecutor(max_workers=PARALLEL_BOARDS) as pool:
        results = list(pool.map(_poll, jobs))

    # c) store sequentially, in watchlist order
    for (company, payload, hours, cache, before), (evals, exc) in zip(jobs, results):
        if cache is not None:
            store.save_details(f"{company.platform}:{company.slug}:",
                               {k: v for k, v in cache.items() if k not in before}, ts)
        if exc is not None:
            log.error("board error: %s:%s %s", company.platform, company.slug, exc)
            totals["errors"] += 1
            if store:
                store.board_error(company.name, company.platform, str(exc), ts)
            continue
        totals["boards_polled"] += 1
        if args.print:
            print(report(evals, company.name, verbose=not args.brief))
            print()
            continue
        status = store.board_ok(company.name, company.platform, len(evals), ts)
        if status != "OK":
            log.warning("%s board: %s", company.name, status)
        if store is not None:
            store.source_complete(f"board:{company.platform}:{company.slug}", ts)
        board_keys |= {dedupe_key(e) for e in evals}
        board_companies.add(normalize(company.name))
        c = store_evaluations(store, company, evals, ts, criteria)
        store.note_board_passes(company.name, c["passed"], ts)
        add_stats(stats, SOURCE_NAMES.get(company.platform, company.platform), c)
        log.info("%s: %d postings, %d new, %d passed", company.name, c["seen"], c["new"], c["passed"])

    # 4. search postings not already on a company board
    if muse_evals is not None:
        c = store_evaluations(store, Company(name="The Muse", platform="muse", slug="search"), muse_evals, ts,
                              criteria, prefix="muse:" if muse_complete else "muse:__none__",
                              board_keys=board_keys, board_companies=board_companies)
        if muse_complete:
            store.source_complete("muse", ts)
        add_stats(stats, "The Muse", c)
        log.info("The Muse: %d postings kept after merging with company boards, %d passed", c["seen"], c["passed"])
    if jsearch_evals is not None:
        c = store_evaluations(store, Company(name="JSearch", platform="jsearch", slug="search"), jsearch_evals, ts,
                              criteria, prefix="jsearch:__none__", board_keys=board_keys,
                              board_companies=board_companies)
        add_stats(stats, "JSearch", c)
        log.info("JSearch: %d postings stored, %d passed", c["seen"], c["passed"])
    if store is not None and not single:
        js = (load_sources_config().get("jsearch") or {})
        store.close_stale("jsearch:", int(js.get("close_after_days_unseen", 7)), ts)
    if store is None:
        return 0
    store.mark_evergreen(int(criteria.get("freshness", "evergreen_after_days", default=60)), ts)
    store.compact()
    for st in stats.values():
        totals["postings_seen"] += st["seen"]
        totals["new"] += st["new"]
        totals["passed"] += st["passed"]
    js = load_sources_config().get("jsearch") or {}
    cov = store.coverage_counts(ts)
    cov.update({"boards_polled_by_platform": dict(Counter(c.platform for c in companies)),
                "jsearch_budget": js.get("requests_per_day", 0),
                "jsearch_pairs_total": len(js.get("keywords", [])) * len(js.get("cities", []))})
    store.record_run(started_at=ts, finished_at=now_iso(), new_passed=sum(s["new_passed"] for s in stats.values()),
                     seconds=round(time.time() - t0, 1), coverage=json.dumps(cov), **totals)
    store.record_run_sources(ts, stats)
    store.commit()
    backend = backend_from_env(args.fake_sheet)
    if backend is None:
        log.info("Sheet sync skipped: set SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON (or --fake-sheet)")
    else:
        res = sync_all(backend, store, criteria, ts)
        log.info("Sheet: %s (%d requests in one write)",
                 ", ".join(f"{t} {n}" for t, n in res["tabs"].items()), res["requests"])
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

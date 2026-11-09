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
from .sources.workday import Listing
from .sources import SourceError, get_source, load_fixture
from .sources.jsearch import JSearchSource, search as jsearch_search
from .sources.muse import MuseSource, muse_locations
from .textmatch import find_terms, normalize

log = logging.getLogger("pipeline")


DETAIL_PLATFORMS = ("smartrecruiters", "workday", "oracle")
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
    out = Listing(evaluate_one(r, company, criteria, gates, matcher) for r in raws)
    out.truncated = getattr(raws, "truncated", False)
    return out


def stub_drops(ev: Evaluation) -> bool:
    """A title/location-only check is final unless its only problem is salary: the pay may
    be in the description it hasn't seen ('Special Inspector' with $45-55/hr inside)."""
    return not ev.passed and any(f.gate != "salary" for f in ev.failures)


def evaluate_one(raw, company, criteria, gates, matcher) -> Evaluation:
    """Rules for one posting. Fast path: a posting that already fails on its title,
    location or job type is not put through the (slow) description parsing."""
    stub = dataclasses.replace(raw, description_lines=[])
    quick = gates.evaluate(normalize_posting(stub, company, criteria, matcher))
    if stub_drops(quick):
        quick.posting.raw = raw            # keep the full posting for the snapshot
        quick.posting.description_lines = raw.description_lines
        try:                               # pay is still worth showing (a job you add stays visible)
            from .pipeline.comp import parse_comp
            loc = quick.posting.location
            quick.posting.comp = parse_comp(raw.description_lines, raw.comp_ranges, raw.comp_extra, criteria,
                                            market=loc.market, region=loc.best.region if loc.best else None,
                                            employment_type=None, matcher=matcher)
        except Exception:
            pass
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
        elif src.platform == "oracle":
            from .sources.oracle import locations_of
            stub = RawPosting(platform=src.platform, slug=company.slug, native_id="stub",
                              title=item.get("Title") or "", url="", locations=locations_of(item),
                              country=item.get("PrimaryLocationCountry") or None)
        else:
            loc = (item.get("location") or {})
            stub = RawPosting(platform=src.platform, slug=company.slug, native_id=str(item["id"]),
                              title=item.get("name") or "", url="",
                              locations=[loc["fullLocation"]] if loc.get("fullLocation") else [],
                              country=(loc.get("country") or "").upper() or None,
                              workplace_type="remote" if loc.get("remote") and not loc.get("hybrid") else None,
                              employment_type=(item.get("typeOfEmployment") or {}).get("label"))
        return not stub_drops(gates.evaluate(normalize_posting(stub, company, criteria, matcher)))

    prefix = f"{src.platform}:{company.slug}:"
    extra = ({"place_ok": lambda d: matcher.match_component(d) is not None}
             if src.platform in ("workday", "oracle") else {})
    if cache is not None:                      # caller loads and saves the cache (parallel polling)
        return src.fetch(company.slug, want=want, cache=cache, **extra)
    cache = store.details_for(prefix) if store is not None else {}
    before = set(cache)
    raws = src.fetch(company.slug, want=want, cache=cache, **extra)
    if store is not None:
        store.save_details(prefix, {k: v for k, v in cache.items() if k not in before}, ts or now_iso())
    return raws


SOURCE_NAMES = {"lever": "Lever", "greenhouse": "Greenhouse", "ashby": "Ashby", "muse": "The Muse", "adzuna": "Adzuna", "jobspipe": "JobsPipe", "serpapi": "Google Jobs (SerpApi)",
                "smartrecruiters": "SmartRecruiters", "jsearch": "JSearch", "workday": "Workday", "oracle": "Oracle", "workable": "Workable"}
SEARCH_PLATFORMS = ("muse", "jsearch", "adzuna", "jobspipe", "serpapi")


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
    how = {"in_office": "in office", "hybrid": "hybrid", "remote": "remote"}.get(p.work_arrangement)
    return {"field": ev.field, "visa": ev.visa, "location": f"{where} · {how}" if how else where,
            "tier": b.tier if b else 9, "salary": p.comp.display(), "extra_comp": extra if extra != "—" else "",
            "experience": p.experience.label(),
            "source": SOURCE_NAMES.get(p.raw.platform, p.raw.platform) if p.raw else ""}


_FIT_CRITERIA = None


def _note_company_fit(store: Store, ev: Evaluation, company, ts: str) -> None:
    """Pipeline 2 feed: a company whose OWN opening description puts it in hospitality or
    real estate (2+ distinct terms, e.g. 'commercial real estate' + 'construction companies')
    is suggested on the Companies tab, whether or not this job fits you. One job's title or
    duties mentioning a hotel or a property is not enough (that added AbbVie, Palantir...)."""
    global _FIT_CRITERIA
    lines = ev.posting.description_lines
    if not lines:
        return
    if _FIT_CRITERIA is None:
        _FIT_CRITERIA = load_criteria()
    ff = _FIT_CRITERIA.get("field_fit", default={}) or {}
    # the company blurb: the text before the first heading, or under an "About ..." heading
    intro, about_sec, cur = [], [], None
    for ln in lines:
        if ln.startswith("## "):
            cur = ln[3:].lower()
            continue
        if cur is None:
            intro.append(ln)
        elif cur.startswith("about") and "role" not in cur and "you" not in cur:
            about_sec.append(ln)
    intro = (intro or about_sec)[:6]
    domain = None
    for d, terms in (ff.get("core_company_terms") or {}).items():
        if len({h.term.lower().rstrip("s") for h in find_terms(intro, terms)}) >= 2:
            domain = "hospitality" if d.startswith("hospitality") else "real estate"
            break
    if not domain:
        return
    ai = bool(find_terms(intro, ff.get("ai_terms") or [], first_only=True))
    fit = f"AI × {domain}" if ai else domain
    about = next((ln for ln in intro if len(ln) > 40), "")
    name = ev.posting.company or company.name
    careers = None if company.platform in SEARCH_PLATFORMS else ev.posting.url
    store.note_target_company(name, fit, about[:220], AUTO_SOURCE, ts, careers)


AUTO_SOURCE = "found by the job pipeline"


def store_evaluations(store: Store, company, evals: list[Evaluation], ts: str, criteria=None,
                      prefix: str | None = None, board_keys: set[str] | None = None,
                      board_companies: set[str] | None = None, close: bool = True) -> dict:
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
        _note_company_fit(store, ev, company, ts)
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
        snapshot = (p.raw.raw if p.raw else {}) if (ev.passed or store.is_tracked(p.posting_id)) else None
        info = store.upsert_posting(p.posting_id, p.company, p.title, p.url, p.posted_at,
                                    snapshot, ts, dedupe_key=key)
        if not is_search:
            store.merge_search_copies(p.posting_id, key, ts)
        seen.append(p.posting_id)
        p.first_seen = info["first_seen"]
        counts["new"] += int(info["is_new"])
        counts["passed"] += int(ev.passed)
        if ev.passed and os.environ.get("PIPELINE_DUMP_PASSED"):
            # review aid: every passing posting's full text, one JSON line each
            with open(os.environ["PIPELINE_DUMP_PASSED"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": p.posting_id, "company": p.company, "title": p.title, "url": p.url,
                                     "lines": p.description_lines}) + "\n")
        counts["new_passed"] += int(ev.passed and info["is_new"])
        if not ev.passed:
            counts["reasons"][ev.failures[0].gate] += 1
        store.save_score(p.posting_id, ev.passed,
                         [{"gate": f.gate, "matched": f.matched} for f in ev.failures],
                         ev.flags, display_fields(ev), ts)
    if close:       # a site we could only partly read never closes the postings we didn't reach
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
    if payload is None and cfg.get("needs_key") and not os.environ.get("MUSE_API_KEY"):
        return None, False              # anonymous requests are blocked since Nov 8: only with your key
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
    # your best searches run every day first (Nov 9); the rest of the budget rotates through the others
    focus = {f"{f['keywords']} in {f['city']}": {"query": f["city"], "country": f.get("country", "us")}
             for f in cfg.get("focus") or []}
    pairs.update(focus)
    today = ts[:10]
    done_today = {r["pair"] for r in store.conn.execute(
        "SELECT pair FROM search_pairs WHERE source='jsearch' AND substr(last_searched, 1, 10)=?", (today,))}
    plan = [(p, False) for p in focus if p not in done_today][:budget]
    plan += store.next_pairs("jsearch", [p for p in pairs if p not in focus and p not in done_today],
                             budget - len(plan))          # never the same search twice in a day
    src, evals, spent = JSearchSource(), [], 0
    sess = requests.Session()
    for pair, first in plan:
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


def run_adzuna(store: Store, cfg: dict, criteria, gates, matcher, ts: str) -> list[Evaluation] | None:
    """Adzuna: today's budget on your focus searches first, then the least-recently searched pairs."""
    from .sources.adzuna import AdzunaSource, search as adzuna_search
    if not cfg.get("enabled") or not (os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")):
        return None
    budget = store.budget_left("adzuna", int(cfg.get("requests_per_day", 0)), ts)
    if budget <= 0:
        return None
    pairs = {f"{kw} in {c}": (kw, c) for kw in cfg.get("keywords", []) for c in cfg.get("cities", [])}
    focus = {f"{f['keywords']} in {f['city']}": (f["keywords"], f["city"]) for f in cfg.get("focus") or []}
    pairs.update(focus)
    done_today = {r["pair"] for r in store.conn.execute(
        "SELECT pair FROM search_pairs WHERE source='adzuna' AND substr(last_searched, 1, 10)=?", (ts[:10],))}
    plan = [(p, False) for p in focus if p not in done_today][:budget]
    plan += store.next_pairs("adzuna", [p for p in pairs if p not in focus and p not in done_today],
                             budget - len(plan))
    src, evals, spent, sess = AdzunaSource(), [], 0, requests.Session()
    for pair, first in plan:
        what, where = pairs[pair]
        try:
            payload = adzuna_search(what, where, max_days_old=int(cfg.get("first_fill_days", 30) if first
                                                                   else cfg.get("max_days_old", 3)), session=sess)
        except SourceError as exc:
            log.error("Adzuna: %s", exc)
            store.board_error("Adzuna (search)", "adzuna", str(exc), ts)
            break
        spent += 1
        raws = src.parse(payload)
        store.mark_pair("adzuna", pair, len(raws), ts)
        evals += evaluate_raws(raws, criteria, gates, matcher)
    store.spend("adzuna", spent, ts)
    store.commit()
    log.info("Adzuna: %d searches, %d postings", spent, len(evals))
    return evals


def run_jobspipe(store: Store, cfg: dict, criteria, gates, matcher, ts: str) -> list[Evaluation] | None:
    """One-time JobsPipe sweep (its free 1,000 credits never renew): each search pages until its
    share is spent; every employer board named in a result's link is watched from then on."""
    from .resolve.discover import board_from_url
    from .sources.jobspipe import JobsPipeSource, search as jobspipe_search
    if not cfg.get("enabled") or not os.environ.get("JOBSPIPE_API_KEY"):
        return None
    used = store.conn.execute("SELECT COALESCE(SUM(used), 0) FROM search_usage WHERE source='jobspipe'").fetchone()[0]
    left = int(cfg.get("total_credits", 950)) - int(used)
    if left < 25:
        return None
    base = dict(cfg.get("filters") or {})
    src, evals, spent, sess = JobsPipeSource(), [], 0, requests.Session()
    for q in cfg.get("searches") or []:
        name = q["name"]
        row = store.conn.execute("SELECT last_count FROM search_pairs WHERE source='jobspipe' AND pair=?",
                                 (name,)).fetchone()
        done = int(row["last_count"]) if row else 0
        want = int(q.get("pages", 1)) * 25 - done
        cursor = None
        while want >= 25 and left >= 25:
            body = {**base, **{k: v for k, v in q.items() if k not in ("name", "pages")}, "limit": 25}
            if cursor:
                body["cursor"] = cursor
            try:
                payload = jobspipe_search(body, sess)
            except SourceError as exc:
                log.error("JobsPipe: %s", exc)
                store.board_error("JobsPipe (search)", "jobspipe", str(exc), ts)
                left = 0
                break
            got = len(payload.get("data") or [])
            spent += got
            left -= got
            want -= 25
            done += got
            raws = src.parse(payload)
            for raw in raws:                      # the employer's own board, watched for free from now on
                b = board_from_url(raw.raw.get("source_url") or "") or board_from_url(raw.url)
                if b and raw.company:
                    store.add_discovered(raw.company, b[0], b[1], "JobsPipe", raw.title, ts)
            evals += evaluate_raws(raws, criteria, gates, matcher)
            cursor = (payload.get("metadata") or {}).get("next_cursor")
            if not cursor or got < 25:
                want = 0                          # this search has no more results
        store.mark_pair("jobspipe", name, max(done, int(q.get("pages", 1)) * 25) if not cursor else done, ts)
    store.spend("jobspipe", spent, ts)
    store.commit()
    log.info("JobsPipe: %d jobs (credits) this run, %d left of the free grant", spent, max(left, 0))
    return evals


def run_serpapi(store: Store, cfg: dict, criteria, gates, matcher, ts: str) -> list[Evaluation] | None:
    """SerpApi Google Jobs: a second free Google Jobs quota. Focus searches first, then rotation."""
    from .sources.serpapi import SerpApiSource, search as serp_search
    if not cfg.get("enabled") or not os.environ.get("SERPAPI_API_KEY"):
        return None
    budget = store.budget_left("serpapi", int(cfg.get("requests_per_day", 0)), ts)
    if budget <= 0:
        return None
    pairs = {f"{kw} in {c}": (kw, c) for kw in cfg.get("keywords", []) for c in cfg.get("cities", [])}
    focus = {f"{f['keywords']} in {f['city']}": (f["keywords"], f["city"]) for f in cfg.get("focus") or []}
    pairs.update(focus)
    done_today = {r["pair"] for r in store.conn.execute(
        "SELECT pair FROM search_pairs WHERE source='serpapi' AND substr(last_searched, 1, 10)=?", (ts[:10],))}
    plan = [(p, False) for p in focus if p not in done_today][:budget]
    plan += store.next_pairs("serpapi", [p for p in pairs if p not in focus and p not in done_today],
                             budget - len(plan))
    src, evals, spent, sess = SerpApiSource(), [], 0, requests.Session()
    for pair, first in plan:
        q, where = pairs[pair]
        try:
            payload = serp_search(q, where, "date_posted:month" if first else "date_posted:3days", sess)
        except SourceError as exc:
            log.error("SerpApi: %s", exc)
            store.board_error("Google Jobs (SerpApi)", "serpapi", str(exc), ts)
            break
        spent += 1
        raws = src.parse(payload)
        store.mark_pair("serpapi", pair, len(raws), ts)
        evals += evaluate_raws(raws, criteria, gates, matcher)
    store.spend("serpapi", spent, ts)
    store.commit()
    log.info("SerpApi: %d searches, %d postings", spent, len(evals))
    return evals


def _refetch_old_search_copies(store: Store, ts: str) -> None:
    """Results stored before full copies were kept: fetch them once (20 per request, from the
    daily JSearch budget) so they can be re-checked too."""
    from .sources.jsearch import job_details
    if not os.environ.get("JSEARCH_API_KEY"):
        return
    rows = store.conn.execute(
        "SELECT p.posting_id, p.snapshot FROM postings p JOIN scores s USING(posting_id) "
        "WHERE p.posting_id LIKE 'jsearch:%' AND p.closed_at IS NULL AND p.merged_into IS NULL AND s.passed = 1 "
        "AND p.snapshot IS NOT NULL").fetchall()
    old = {r["posting_id"].split(":", 2)[2]: r for r in rows if "job" not in (json.loads(r["snapshot"]) or {})}
    per_day = int((load_sources_config().get("jsearch") or {}).get("requests_per_day", 0))
    if not old or store.budget_left("jsearch", per_day, ts) <= 0:
        return
    ids = list(old)[:20]
    try:
        data = job_details(ids)
    except SourceError as exc:
        log.warning("JSearch re-fetch: %s", exc)
        return
    store.spend("jsearch", 1, ts)
    got = {str(j.get("job_id")): j for j in data.get("data") or []}
    for jid in ids:
        r = old[jid]
        snap = json.loads(r["snapshot"]) or {}
        if jid in got:
            snap["job"] = got[jid]
            store.conn.execute("UPDATE postings SET snapshot=? WHERE posting_id=?", (json.dumps(snap), r["posting_id"]))
        else:       # Google no longer lists it: treat as closed
            store.conn.execute("UPDATE postings SET closed_at=? WHERE posting_id=?", (ts, r["posting_id"]))
    store.commit()
    log.info("JSearch re-fetch: %d stored results refreshed, %d no longer listed", len(got), len(ids) - len(got))


def recheck_search_postings(store: Store, criteria, gates, matcher, ts: str) -> int:
    """Open JSearch results are only fetched once; re-apply today's rules to them every run
    (from the stored copy) so a rule fix also removes search results that no longer qualify."""
    src, changed = JSearchSource(), 0
    _refetch_old_search_copies(store, ts)
    rows = store.conn.execute(
        "SELECT p.posting_id, p.company, p.title, p.url, p.snapshot FROM postings p JOIN scores s USING(posting_id) "
        "WHERE p.posting_id LIKE 'jsearch:%' AND p.closed_at IS NULL AND p.merged_into IS NULL "
        "AND s.passed = 1").fetchall()
    for r in rows:
        try:
            job = (json.loads(r["snapshot"] or "null") or {}).get("job")
            if not job:
                # stored before full copies were kept: the title alone still decides most
                # rules (field, level, job type), so a title that fails today's rules drops
                from .sources.base import RawPosting
                stub = RawPosting(platform="jsearch", slug="", native_id=r["posting_id"].split(":", 2)[2],
                                  title=r["title"] or "", url=r["url"] or "", locations=[])
                company = Company(name=r["company"] or "", platform="jsearch", slug="")
                quick = gates.evaluate(normalize_posting(stub, company, criteria, matcher))
                title_fails = [f for f in quick.failures if f.gate not in ("salary", "location")]
                if title_fails:
                    changed += 1
                    store.save_score(r["posting_id"], False,
                                     [{"gate": f.gate, "matched": f.matched} for f in title_fails],
                                     quick.flags, display_fields(quick), ts)
                continue
            raw = src.parse({"data": [job]})[0]
            company = Company(name=r["company"] or raw.company or "", platform="jsearch", slug=raw.slug)
            ev = evaluate_one(raw, company, criteria, gates, matcher)
        except Exception as exc:      # one bad stored copy never stops the run
            log.warning("re-check %s: %s", r["posting_id"], exc)
            continue
        if not ev.passed:
            changed += 1
        store.save_score(r["posting_id"], ev.passed, [{"gate": f.gate, "matched": f.matched} for f in ev.failures],
                         ev.flags, display_fields(ev), ts)
    # The Muse: stored copies are the full result, so they re-check the same way
    from .textmatch import parse_when
    muse_rows = store.conn.execute(
        "SELECT p.posting_id, p.company, p.snapshot FROM postings p JOIN scores s USING(posting_id) "
        "WHERE p.posting_id LIKE 'muse:%' AND p.closed_at IS NULL AND p.merged_into IS NULL "
        "AND s.passed = 1 AND p.snapshot IS NOT NULL").fetchall()
    muse = MuseSource(load_sources_config().get("muse") or {})
    for r in muse_rows:
        try:
            snap = json.loads(r["snapshot"]) or {}
            parsed = muse.parse({"results": [snap]}, now=parse_when(ts)) if snap.get("name") else []
            if not parsed:
                continue          # too old to parse: it closes with the next Muse sweep
            raw = parsed[0]
            company = Company(name=r["company"] or raw.company or "", platform="muse", slug=raw.slug)
            ev = evaluate_one(raw, company, criteria, gates, matcher)
        except Exception as exc:      # one bad stored copy never stops the run
            log.warning("re-check %s: %s", r["posting_id"], exc)
            continue
        if not ev.passed:
            changed += 1
        store.save_score(r["posting_id"], ev.passed, [{"gate": f.gate, "matched": f.matched} for f in ev.failures],
                         ev.flags, display_fields(ev), ts)
    # Adzuna / SerpApi / JobsPipe: the stored copy is the full result, so today's rules apply to it too
    from .sources.adzuna import AdzunaSource
    from .sources.jobspipe import JobsPipeSource
    from .sources.serpapi import SerpApiSource
    parsers = {"adzuna": lambda j: AdzunaSource().parse({"results": [j]}),
               "serpapi": lambda j: SerpApiSource().parse({"jobs_results": [j]}),
               "jobspipe": lambda j: JobsPipeSource().parse({"data": [j]})}
    for prefix, parse in parsers.items():
        for r in store.conn.execute(
                "SELECT p.posting_id, p.company, p.snapshot FROM postings p JOIN scores s USING(posting_id) "
                f"WHERE p.posting_id LIKE '{prefix}:%' AND p.closed_at IS NULL AND p.merged_into IS NULL "
                "AND s.passed = 1 AND p.snapshot IS NOT NULL").fetchall():
            try:
                job = (json.loads(r["snapshot"]) or {}).get("job")
                if not job:
                    continue
                parsed = parse(job)
                if not parsed:                       # e.g. the "employer" is a job site
                    changed += 1
                    store.save_score(r["posting_id"], False, [{"gate": "not_a_real_opening",
                                                               "matched": "a job site, not the employer"}], [], {}, ts)
                    continue
                raw = parsed[0]
                ev = evaluate_one(raw, Company(name=r["company"] or raw.company or "", platform=prefix, slug=raw.slug),
                                  criteria, gates, matcher)
            except Exception as exc:      # one bad stored copy never stops the run
                log.warning("re-check %s: %s", r["posting_id"], exc)
                continue
            if not ev.passed:
                changed += 1
            store.save_score(r["posting_id"], ev.passed, [{"gate": f.gate, "matched": f.matched} for f in ev.failures],
                             ev.flags, display_fields(ev), ts)
    store.commit()
    if changed:
        log.info("Re-checked search results: %d no longer meet the must-haves", changed)
    return changed


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
    found = _add_verified_boards(store, candidates, known, ts, "hn", "Hacker News", cfg)
    log.info("Hacker News: %d board links in posts about your cities, %d new boards", len(candidates), len(found))
    return found


def _add_verified_boards(store: Store, candidates: dict[tuple[str, str], str], known: set[str], ts: str,
                         probe_prefix: str, via: str, cfg: dict) -> list[Company]:
    """Check each candidate board with one request and add the live ones to the watch."""
    todo = [(b, c) for b, c in candidates.items()
            if normalize(c) not in known and store.probe_due(f"{probe_prefix}:{b[0]}:{b[1]}", 30, ts)]
    cap = cfg.get("max_new_boards_per_run")
    if cap:
        todo = todo[: int(cap)]
    with ThreadPoolExecutor(max_workers=PARALLEL_BOARDS) as pool:
        counts = list(pool.map(lambda bc: verify_board(bc[0][0], bc[0][1], requests.Session()), todo))
    found = []
    for ((platform, slug), company), n in zip(todo, counts):
        store.record_probe(f"{probe_prefix}:{platform}:{slug}", f"{n} postings", ts)
        if n and normalize(company) not in known:
            store.add_discovered(company, platform, slug, via, "", ts)
            known.add(normalize(company))
            found.append(Company(name=company, platform=platform, slug=slug))
            log.info("Discovered %s board for %s from %s (%d postings)", platform, company, via, n)
    store.commit()
    return found


def discover_from_newgrad_lists(store: Store, cfg: dict, known: set[str], matcher, ts: str) -> list[Company]:
    """Company boards behind community new-grad lists (listings in your cities only)."""
    from urllib.parse import urlparse
    from .resolve.discover import _GH_JOB_ID as greenhouse_behind_re, greenhouse_behind
    from .sources import newgrad_lists
    sess = requests.Session()
    candidates: dict[tuple[str, str], str] = {}
    hidden: dict[str, str] = {}
    local = 0
    for url in cfg.get("urls", []) or []:
        for x in newgrad_lists.open_listings(newgrad_lists.fetch(url, sess)):
            if not matcher.match(x["locations"]).offices:
                continue
            local += 1
            b = board_from_url(x["url"])
            if b:
                candidates.setdefault(b, x["company"])
            elif greenhouse_behind_re.search(x["url"]) and normalize(x["company"]) not in known:
                hidden.setdefault(x["company"], x["url"])     # career page with Greenhouse behind it
            else:
                store.count_link_domain(urlparse(x["url"]).netloc.lower())
    todo = [(c, u) for c, u in hidden.items() if store.probe_due(f"ghjid:{normalize(c)}", 30, ts)]
    with ThreadPoolExecutor(max_workers=PARALLEL_BOARDS) as pool:
        slugs = list(pool.map(lambda cu: greenhouse_behind(cu[1], cu[0], requests.Session()), todo))
    for (company, _), slug in zip(todo, slugs):
        store.record_probe(f"ghjid:{normalize(company)}", slug or "not found", ts)
        if slug:
            candidates.setdefault(("greenhouse", slug), company)
    found = _add_verified_boards(store, candidates, known, ts, "newgrad", "New-grad lists", cfg)
    log.info("New-grad lists: %d open listings in your cities, %d company boards, %d new", local, len(candidates), len(found))
    return found


YC_HIRING = "https://yc-oss.github.io/api/companies/hiring.json"     # YC's public directory (open dataset)


def yc_candidates(companies: list[dict], matcher) -> dict[tuple[str, str], str]:
    """(platform, slug) -> company for YC startups hiring in your cities. A board name is guessed
    from the company's website / name; short generic names count only when they ARE the website
    name (radar.com -> 'radar'), so a different company's board isn't picked up."""
    from urllib.parse import urlparse
    from .resolve.discover import candidate_slugs
    out: dict[tuple[str, str], str] = {}
    for c in companies:
        places = [p.strip() for p in (c.get("all_locations") or "").split(";") if p.strip()]
        if not places or not matcher.match(places).offices:
            continue
        host = urlparse(c.get("website") or "").netloc.lower().removeprefix("www.")
        stem = host.split(".")[0] if host else ""
        for slug in candidate_slugs(c.get("name") or "", stem or None)[:3]:
            if slug != stem and len(slug) < 7:
                continue
            for platform in ("ashby", "greenhouse", "lever"):
                out.setdefault((platform, slug), c["name"])
    return out


def discover_from_yc(store: Store, cfg: dict, known: set[str], matcher, ts: str) -> list[Company]:
    """Job boards of YC startups that are hiring in your cities (Nov 9: more startup jobs)."""
    r = requests.get(cfg.get("url") or YC_HIRING, timeout=60, headers={"User-Agent": "job-pipeline"})
    r.raise_for_status()
    cands = yc_candidates(r.json(), matcher)
    # one board per company: drop guesses for companies already watched, then verify
    found = _add_verified_boards(store, cands, known, ts, "yc", "YC directory", cfg)
    log.info("YC directory: %d board guesses for startups in your cities, %d new boards", len(cands), len(found))
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


def probe(spec: str) -> int:
    """Fetch one board with the real rules and print what it found (no database, no Sheet)."""
    import collections
    platform, _, slug = spec.partition(":")
    criteria = load_criteria()
    company = Company(name=f"probe {slug}", platform=platform, slug=slug)
    evals = evaluate_company(company, None, criteria, Gates(criteria), LocationMatcher(criteria), None, now_iso(), {})
    passed = [e for e in evals if e.passed]
    reasons = collections.Counter(e.failures[0].gate for e in evals if not e.passed)
    print(f"{spec}: {len(evals)} postings, {len(passed)} pass, truncated={getattr(evals, 'truncated', False)}")
    print("drops:", dict(reasons.most_common()))
    for e in passed[:40]:
        p = e.posting
        print(f"  PASS {p.title[:60]} | {', '.join(p.locations)[:40]} | {p.comp.matched_text[:40] if p.comp.known else 'no pay'}"
              f" | {p.experience.label()} | desc lines {len(p.description_lines)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Job pipeline")
    ap.add_argument("--print", action="store_true", help="print a console report (step 1)")
    ap.add_argument("--company", help="only this watchlist company")
    ap.add_argument("--fixture", help="use a saved API response instead of the live board")
    ap.add_argument("--brief", action="store_true", help="omit the dropped list")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path")
    ap.add_argument("--probe", help="check one board, e.g. oracle:jpmc.fa.oraclecloud.com/CX_1001, and print a summary")
    ap.add_argument("--fake-sheet", help="write the Sheet to this local JSON file instead of Google")
    args = ap.parse_args(argv)
    if args.probe:
        return probe(args.probe)
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
    muse_evals, muse_complete, jsearch_evals, adzuna_evals, jobspipe_evals = (None, False, None, None, None)
    if store is not None and not single:
        cfg = load_sources_config()
        muse_evals, muse_complete = run_muse(store, cfg.get("muse") or {}, criteria, gates, matcher, ts)
        recheck_search_postings(store, criteria, gates, matcher, ts)     # before today's searches use the budget
        jsearch_evals = run_jsearch(store, cfg.get("jsearch") or {}, criteria, gates, matcher, ts)
        try:
            adzuna_evals = run_adzuna(store, cfg.get("adzuna") or {}, criteria, gates, matcher, ts)
        except Exception as exc:  # a new source never stops the run
            log.error("Adzuna error: %s", exc)
        try:
            serp_evals = run_serpapi(store, cfg.get("serpapi") or {}, criteria, gates, matcher, ts)
            jsearch_evals = (jsearch_evals or []) + serp_evals if serp_evals else jsearch_evals
        except Exception as exc:  # a new source never stops the run
            log.error("SerpApi error: %s", exc)
        try:
            jobspipe_evals = run_jobspipe(store, cfg.get("jobspipe") or {}, criteria, gates, matcher, ts)
        except Exception as exc:  # a new source never stops the run
            log.error("JobsPipe error: %s", exc)
        known = {normalize(c.name) for c in companies} | {normalize(r["company"]) for r in store.discovered_boards()}
        if muse_evals or jsearch_evals or adzuna_evals or jobspipe_evals:
            try:
                discover(store, (muse_evals or []) + (jsearch_evals or []) + (adzuna_evals or []) + (jobspipe_evals or []),
                         cfg.get("discovery") or {}, known, ts)
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
        ng_cfg = cfg.get("newgrad_lists") or {}
        if ng_cfg.get("enabled") and store.source_due("newgrad_lists", ng_cfg.get("every_hours", 24), ts):
            try:
                discover_from_newgrad_lists(store, ng_cfg, known, matcher, ts)
                store.source_complete("newgrad_lists", ts)
            except Exception as exc:  # never stops the run
                log.error("New-grad list discovery error: %s", exc)
        yc_cfg = cfg.get("yc") or {}
        if yc_cfg.get("enabled") and store.source_due("yc", yc_cfg.get("every_hours", 24), ts):
            try:
                discover_from_yc(store, yc_cfg, known, matcher, ts)
                store.source_complete("yc", ts)
            except Exception as exc:  # never stops the run
                log.error("YC discovery error: %s", exc)
        # pipeline 2: a target company whose careers page is a readable job board is watched
        try:
            from .output.sheet import load_target_companies
            from .resolve.discover import board_from_url
            for c in load_target_companies():
                b = board_from_url(c.get("careers") or "")
                if b and c.get("name"):
                    store.add_discovered(c["name"], b[0], b[1], "target company list", "", ts)
            store.commit()
        except Exception as exc:  # never stops the run
            log.error("target company boards: %s", exc)
        watch = {normalize(c.name) for c in companies}
        watched_boards = {(c.platform, c.slug.lower()) for c in companies}
        companies += [Company(name=r["company"], platform=r["platform"], slug=r["slug"])
                      for r in store.discovered_boards()
                      if normalize(r["company"]) not in watch and (r["platform"], r["slug"].lower()) not in watched_boards]

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
        if getattr(evals, "truncated", False):
            log.warning("%s: more postings than one poll can read; nothing marked closed", company.name)
        c = store_evaluations(store, company, evals, ts, criteria, close=not getattr(evals, "truncated", False))
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
    if adzuna_evals is not None:
        c = store_evaluations(store, Company(name="Adzuna", platform="adzuna", slug="search"), adzuna_evals, ts,
                              criteria, prefix="adzuna:__none__", board_keys=board_keys,
                              board_companies=board_companies)
        add_stats(stats, "Adzuna", c)
        log.info("Adzuna: %d postings stored, %d passed", c["seen"], c["passed"])
    if jobspipe_evals is not None:                # one-time sweep: stored, never closed for being "unseen"
        c = store_evaluations(store, Company(name="JobsPipe", platform="jobspipe", slug="search"), jobspipe_evals, ts,
                              criteria, prefix="jobspipe:__none__", board_keys=board_keys,
                              board_companies=board_companies)
        add_stats(stats, "JobsPipe", c)
        log.info("JobsPipe: %d postings stored, %d passed", c["seen"], c["passed"])
    if store is not None and not single:
        js = (load_sources_config().get("jsearch") or {})
        store.close_stale("jsearch:", int(js.get("close_after_days_unseen", 7)), ts)
        store.close_stale("serpapi:", int(js.get("close_after_days_unseen", 7)), ts)
        store.close_stale("adzuna:", int((load_sources_config().get("adzuna") or {}).get("close_after_days_unseen", 7)), ts)
    if store is None:
        return 0
    store.mark_evergreen(int(criteria.get("freshness", "evergreen_after_days", default=180)), ts)
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

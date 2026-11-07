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
import logging
import re
import sys
from pathlib import Path

import requests

from .config import CONFIG_DIR, Company, load_criteria, load_watchlist, load_yaml
from .output.console import report
from .output.sheet import backend_from_env, sync_all
from .store.db import DEFAULT_DB, Store, now_iso
from .pipeline.gates import Evaluation, Gates
from .pipeline.locations import LocationMatcher
from .pipeline.normalize import normalize_posting
from .resolve.discover import find_board
from .sources import SourceError, get_source, load_fixture
from .sources.muse import MuseSource, muse_locations
from .textmatch import normalize

log = logging.getLogger("pipeline")


def evaluate_company(company, payload=None, criteria=None, gates=None, matcher=None) -> list[Evaluation]:
    criteria = criteria or load_criteria()
    matcher = matcher or LocationMatcher(criteria)
    gates = gates or Gates(criteria)
    src = get_source(company.platform)
    raws = src.parse(payload, company.slug) if payload is not None else src.fetch(company.slug)
    return [gates.evaluate(normalize_posting(r, company, criteria, matcher)) for r in raws]


SOURCE_NAMES = {"lever": "Lever", "greenhouse": "Greenhouse", "ashby": "Ashby", "muse": "The Muse"}


def evaluate_raws(raws, criteria, gates, matcher, company: Company | None = None) -> list[Evaluation]:
    out = []
    for r in raws:
        comp = company or Company(name=r.company or r.slug, platform=r.platform, slug=r.slug)
        out.append(gates.evaluate(normalize_posting(r, comp, criteria, matcher)))
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
    counts = {"seen": len(evals), "new": 0, "passed": 0}
    is_search = company.platform == "muse"
    if not is_search:
        store.upsert_company(company.name, company.platform, company.slug)
    seen = []
    for ev in evals:
        p = ev.posting
        key = dedupe_key(ev)
        if is_search and ((board_keys and key in board_keys) or store.open_board_key(key)):
            continue        # the company-board copy is stored instead
        if is_search and board_companies and normalize(p.company) in board_companies:
            continue        # we watch this company's board and the job isn't on it: not active
        info = store.upsert_posting(p.posting_id, p.company, p.title, p.url, p.posted_at,
                                    p.raw.raw if p.raw else {}, ts, dedupe_key=key)
        if not is_search:
            store.merge_search_copies(p.posting_id, key, ts)
        seen.append(p.posting_id)
        p.first_seen = info["first_seen"]
        counts["new"] += int(info["is_new"])
        counts["passed"] += int(ev.passed)
        store.save_score(p.posting_id, ev.passed,
                         [{"gate": f.gate, "matched": f.matched} for f in ev.failures],
                         ev.flags, display_fields(ev), ts)
    store.close_missing(prefix or f"{company.platform}:{company.slug}:", seen, ts)
    return counts


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


def discover(store: Store, evals: list[Evaluation], cfg: dict, known: set[str], ts: str) -> list[Company]:
    """Probe for the company boards behind search results. Returns newly found boards."""
    if not cfg.get("enabled", True):
        return []
    by_company: dict[str, dict] = {}
    for ev in evals:
        if not ev.posting.location.offices:
            continue   # only companies hiring in your cities
        p = ev.posting
        entry = by_company.setdefault(p.company, {"titles": [], "hint": p.raw.slug if p.raw else None})
        entry["titles"].append(p.title)
    found, probed = [], 0
    sess = requests.Session()
    for name, info in sorted(by_company.items()):
        if normalize(name) in known or not store.probe_due(name, int(cfg.get("retry_unfound_after_days", 30)), ts):
            continue
        if probed >= int(cfg.get("max_new_companies_per_run", 40)):
            break
        probed += 1
        hit = find_board(name, info["titles"], info["hint"], sess)
        store.record_probe(name, f"{hit.platform}:{hit.slug}" if hit else "none", ts)
        if hit:
            store.add_discovered(name, hit.platform, hit.slug, "The Muse", hit.matched_title, ts)
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

    # 1–2. search sources + discovery (full runs only)
    muse_evals, muse_complete = (None, False)
    if store is not None and not single:
        cfg = load_sources_config()
        muse_evals, muse_complete = run_muse(store, cfg.get("muse") or {}, criteria, gates, matcher, ts)
        known = {normalize(c.name) for c in companies} | {normalize(r["company"]) for r in store.discovered_boards()}
        if muse_evals:
            discover(store, muse_evals, cfg.get("discovery") or {}, known, ts)
        watch = {normalize(c.name) for c in companies}
        companies += [Company(name=r["company"], platform=r["platform"], slug=r["slug"])
                      for r in store.discovered_boards() if normalize(r["company"]) not in watch]

    # 3. company boards
    board_keys: set[str] = set()
    board_companies: set[str] = set()
    for company in companies:
        payload = load_fixture(args.fixture) if args.fixture else None
        try:
            evals = evaluate_company(company, payload, criteria, gates, matcher)
        except SourceError as exc:
            log.error("board error: %s", exc)
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
        board_keys |= {dedupe_key(e) for e in evals}
        board_companies.add(normalize(company.name))
        c = store_evaluations(store, company, evals, ts, criteria)
        totals["postings_seen"] += c["seen"]
        for k in ("new", "passed"):
            totals[k] += c[k]
        log.info("%s: %d postings, %d new, %d passed", company.name, c["seen"], c["new"], c["passed"])

    # 4. search postings not already on a company board
    if muse_evals is not None:
        c = store_evaluations(store, Company(name="The Muse", platform="muse", slug="search"), muse_evals, ts,
                              criteria, prefix="muse:" if muse_complete else "muse:__none__",
                              board_keys=board_keys, board_companies=board_companies)
        if muse_complete:
            store.source_complete("muse", ts)
        totals["postings_seen"] += c["seen"]
        for k in ("new", "passed"):
            totals[k] += c[k]
        log.info("The Muse: %d postings kept after merging with company boards, %d passed", c["seen"], c["passed"])
    if store is None:
        return 0
    store.mark_evergreen(int(criteria.get("freshness", "evergreen_after_days", default=60)), ts)
    store.record_run(started_at=ts, finished_at=now_iso(), **totals)
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

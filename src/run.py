"""Entry point: python -m src.run

Each run: poll every watchlist board -> normalize -> gates -> requirements
match -> SQLite (data/pipeline.db) -> Google Sheet (if SHEET_ID and
GOOGLE_SERVICE_ACCOUNT_JSON are set). Signals and tiers plug in at step 4.

  python -m src.run                                  # full run
  python -m src.run --print --company Palantir       # console report only
  python -m src.run --company Palantir --fixture tests/fixtures/lever_palantir.json.gz
  python -m src.run --fake-sheet data/fake_sheet.json   # write to a local stand-in Sheet
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load_criteria, load_watchlist
from .output.console import report
from .output.sheet import backend_from_env, sync_all
from .store.db import DEFAULT_DB, Store, now_iso
from .pipeline.gates import Evaluation, Gates
from .pipeline.locations import LocationMatcher
from .pipeline.normalize import normalize_posting
from .pipeline.signals import SignalScorer
from .pipeline.tiers import assign_tier
from .sources import SourceError, get_source, load_fixture

log = logging.getLogger("pipeline")


def evaluate_company(company, payload=None, criteria=None, gates=None, matcher=None) -> list[Evaluation]:
    criteria = criteria or load_criteria()
    matcher = matcher or LocationMatcher(criteria)
    gates = gates or Gates(criteria)
    src = get_source(company.platform)
    raws = src.parse(payload, company.slug) if payload is not None else src.fetch(company.slug)
    return [gates.evaluate(normalize_posting(r, company, criteria, matcher)) for r in raws]


def display_fields(ev: Evaluation) -> dict:
    p = ev.posting
    b = p.location.best
    where = f"{b.city} (tier {b.tier})" if b else "; ".join(p.locations)
    return {"anchor": p.anchor, "location": f"{where} · {p.work_arrangement}",
            "salary": p.comp.display(), "extra_comp": p.comp.extra_comp_display,
            "experience": p.experience.label()}


def rank(evals: list[Evaluation], criteria=None) -> dict:
    """Signals + tiers for every posting that passed the gates. Returns id -> (Signals, TierResult)."""
    criteria = criteria or load_criteria()
    scorer = SignalScorer(criteria)
    out = {}
    for ev in evals:
        if ev.passed:
            sig = scorer.score(ev)
            out[ev.posting.posting_id] = (sig, assign_tier(ev, sig, criteria))
    return out


def store_evaluations(store: Store, company, evals: list[Evaluation], ts: str, criteria=None) -> dict:
    counts = {"seen": len(evals), "new": 0, "passed": 0, "strong": 0, "worth_a_look": 0}
    ranked = rank(evals, criteria)
    store.upsert_company(company.name, company.platform, company.slug, company.anchor)
    for ev in evals:
        p = ev.posting
        info = store.upsert_posting(p.posting_id, p.company, p.title, p.url, p.posted_at,
                                    p.raw.raw if p.raw else {}, ts)
        p.first_seen = info["first_seen"]
        counts["new"] += int(info["is_new"])
        counts["passed"] += int(ev.passed)
        sig, t = ranked.get(p.posting_id, (None, None))
        if t:
            counts["strong"] += t.tier == "Strong"
            counts["worth_a_look"] += t.tier == "Worth a look"
        store.save_score(
            p.posting_id, ev.passed, [{"gate": f.gate, "matched": f.matched} for f in ev.failures],
            ev.flags, ev.requirements.summary, ev.requirements.score,
            ev.tn.label if ev.tn else "", display_fields(ev), ts,
            role_type=sig.role_type if sig else None, final_score=t.fit_score if t else None,
            tier=t.tier if t else None, why=t.why if t else None, sort_score=t.sort_score if t else None)
    store.close_missing(company.name, [e.posting.posting_id for e in evals], ts)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Job pipeline")
    ap.add_argument("--print", action="store_true", help="print a console report (step 1)")
    ap.add_argument("--company", help="only this watchlist company")
    ap.add_argument("--fixture", help="use a saved API response instead of the live board")
    ap.add_argument("--brief", action="store_true", help="omit per-requirement breakdown")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path")
    ap.add_argument("--fake-sheet", help="write the Sheet to this local JSON file instead of Google")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    criteria = load_criteria()
    matcher = LocationMatcher(criteria)
    gates = Gates(criteria)
    companies = load_watchlist()
    if args.company:
        companies = [c for c in companies if c.name.lower() == args.company.lower()]
        if not companies:
            log.error("company %s not in config/watchlist.yaml", args.company)
            return 2
    ts = now_iso()
    store = None if args.print else Store(args.db)
    totals = {"boards_polled": 0, "postings_seen": 0, "new": 0, "passed": 0, "strong": 0,
              "worth_a_look": 0, "errors": 0}
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
            print(report(evals, company.name, verbose_items=not args.brief, ranked=rank(evals, criteria)))
            print()
            continue
        status = store.board_ok(company.name, company.platform, len(evals), ts)
        if status != "OK":
            log.warning("%s board: %s", company.name, status)
        c = store_evaluations(store, company, evals, ts, criteria)
        totals["postings_seen"] += c["seen"]
        for k in ("new", "passed", "strong", "worth_a_look"):
            totals[k] += c[k]
        log.info("%s: %d postings, %d new, %d passed gates", company.name, c["seen"], c["new"], c["passed"])
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
        log.info("Sheet: %d rows in Pipeline (%d batch requests in one call)", res["rows"], res["requests"])
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

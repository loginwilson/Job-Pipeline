"""Entry point: python -m src.run

Order-of-work step 1: sources -> normalize -> gates -> requirements match ->
console report. Store, Sheet sync, signals and tiers plug in at later steps.

  python -m src.run --print                     # every watchlist company, live
  python -m src.run --print --company Palantir  # one company, live
  python -m src.run --print --company Palantir --fixture tests/fixtures/lever_palantir.json.gz
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load_criteria, load_watchlist
from .output.console import report
from .pipeline.gates import Evaluation, Gates
from .pipeline.locations import LocationMatcher
from .pipeline.normalize import normalize_posting
from .sources import SourceError, get_source, load_fixture

log = logging.getLogger("pipeline")


def evaluate_company(company, payload=None, criteria=None, gates=None, matcher=None) -> list[Evaluation]:
    criteria = criteria or load_criteria()
    matcher = matcher or LocationMatcher(criteria)
    gates = gates or Gates(criteria)
    src = get_source(company.platform)
    raws = src.parse(payload, company.slug) if payload is not None else src.fetch(company.slug)
    return [gates.evaluate(normalize_posting(r, company, criteria, matcher)) for r in raws]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Job pipeline")
    ap.add_argument("--print", action="store_true", help="print a console report (step 1)")
    ap.add_argument("--company", help="only this watchlist company")
    ap.add_argument("--fixture", help="use a saved API response instead of the live board")
    ap.add_argument("--brief", action="store_true", help="omit per-requirement breakdown")
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
    for company in companies:
        payload = load_fixture(args.fixture) if args.fixture else None
        try:
            evals = evaluate_company(company, payload, criteria, gates, matcher)
        except SourceError as exc:
            log.error("board error: %s", exc)
            continue
        if args.print:
            print(report(evals, company.name, verbose_items=not args.brief))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""BUILD_BRIEF Step 8 acceptance checks on the saved Palantir fixture (2026-11-05).

The live postings differ from the brief's expectations in two places and
criteria.yaml wins (see README "Palantir check"):
  * both Warp Speed Deployment Strategist postings say "U.S. Person Required"
    -> work_authorization drop ("U.S. persons" is a drop term)
  * "Deployment Strategist, New Grad - Warp Speed" pays $110K-$120K
    (midpoint $115K < $120K extra-comp floor) -> salary drop
"""
import re

import pytest
from conftest import FIXTURES

from src.config import load_watchlist
from src.run import evaluate_company
from src.sources import load_fixture


@pytest.fixture(scope="module")
def evals():
    palantir = next(c for c in load_watchlist() if c.name == "Palantir")
    return evaluate_company(palantir, load_fixture(FIXTURES / "lever_palantir.json.gz"))


def find(evals, title, loc="New York, NY"):
    return next(e for e in evals if e.posting.title == title and loc in e.posting.locations)


def gates(e):
    return {f.gate for f in e.failures}


def test_expected_keeps(evals):
    assert find(evals, "Product Designer, New Grad").passed
    assert find(evals, "Deployment Strategist").passed
    assert find(evals, "Deployment Strategist", "Chicago, IL").passed


def test_warp_speed_follow_criteria(evals):
    ws = find(evals, "Deployment Strategist - Warp Speed")
    assert gates(ws) == {"work_authorization"}
    assert "U.S. Person" in ws.failures[0].matched
    ng = find(evals, "Deployment Strategist, New Grad - Warp Speed")
    assert {"salary", "work_authorization"} <= gates(ng)


def test_engineer_titles_dropped(evals):
    eng = [e for e in evals if re.search(r"engineer", e.posting.title, re.I)]
    assert eng and all("engineering_title" in gates(e) for e in eng)


def test_gov_defense_dropped(evals):
    gov = [e for e in evals if re.search(r"US Government|Defense", e.posting.title)]
    assert gov and all("gov_defense" in gates(e) for e in gov)


def test_internships_dropped(evals):
    interns = [e for e in evals if re.search(r"\bintern(?:ship)?s?\b", e.posting.title, re.I)
               or e.posting.raw.employment_type == "Internship"]
    assert interns and all("employment_type" in gates(e) for e in interns)


def test_kept_us_postings_have_tn_category(evals):
    kept = [e for e in evals if e.passed and e.posting.is_us]
    assert kept and all(e.tn and e.tn.label.startswith("TN:") for e in kept)


def test_requirements_summary_printed_for_kept(evals):
    for e in (x for x in evals if x.passed):
        assert e.requirements.summary.startswith(("Met ", "No requirements"))

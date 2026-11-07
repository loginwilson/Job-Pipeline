"""Must-have rules on the saved Palantir fixture (2026-11-05)."""
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


def test_passed_set(evals):
    passed = {(e.posting.title, e.posting.locations[0]) for e in evals if e.passed}
    assert passed == {
        ("Product Designer, New Grad", "New York, NY"),
        ("Deployment Strategist", "New York, NY"),
        ("Deployment Strategist", "Chicago, IL"),
        ("Neurodivergent Fellowship", "New York, NY"),
        ("Compliance Analyst", "New York, NY"),
        ("Talent Strategist", "New York, NY"),
        ("Talent Program and Events Strategist", "New York, NY"),
        ("Mobility Tax Analyst", "Palo Alto, CA"),
    }


def test_low_base_flagged_not_dropped(evals):
    assert "Low base — check incentives" in find(evals, "Talent Strategist").flags
    assert "Salary unknown" in find(evals, "Deployment Strategist", "Chicago, IL").flags


def test_warp_speed_needs_us_person(evals):
    for title in ("Deployment Strategist - Warp Speed", "Deployment Strategist, New Grad - Warp Speed"):
        e = find(evals, title)
        assert "work_authorization" in gates(e) and "U.S. Person" in e.failures[0].matched


def test_engineer_titles_dropped(evals):
    eng = [e for e in evals if re.search(r"engineer|developer", e.posting.title, re.I)]
    assert eng and all("technical" in gates(e) for e in eng)


def test_government_roles_never_pass(evals):
    gov = [e for e in evals if re.search(r"US Government|Defense", e.posting.title)]
    assert gov and not any(e.passed for e in gov)


def test_internships_dropped(evals):
    interns = [e for e in evals if re.search(r"\bintern(?:ship)?s?\b", e.posting.title, re.I)
               or e.posting.raw.employment_type == "Internship"]
    assert interns and all("employment_type" in gates(e) for e in interns)


def test_law_degree_dropped(evals):
    assert "credentials" in gates(find(evals, "Finance Operator"))


def test_passed_us_postings_show_visa(evals):
    for e in (x for x in evals if x.passed):
        assert e.visa.startswith("OPT year 1") and e.field

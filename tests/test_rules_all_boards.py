"""Criteria-driven checks across every saved board (no company is special).

Instead of an expected list for one company, these assert the must-haves
hold for EVERY posting that passes, and that every drop explains itself.
Thresholds and terms are read from criteria.yaml.
"""
import re

import pytest
from conftest import FIXTURES

from src.config import Company
from src.run import evaluate_company
from src.sources import load_fixture
from src.textmatch import find_terms

BOARDS = [("lever", "lever_palantir"), ("greenhouse", "greenhouse_withcoverage"), ("greenhouse", "greenhouse_figma"),
          ("ashby", "ashby_ramp"), ("ashby", "ashby_eliseai")]


@pytest.fixture(scope="module")
def evals():
    out = []
    for platform, fixture in BOARDS:
        company = Company(name=fixture, platform=platform, slug=fixture)
        out += evaluate_company(company, load_fixture(FIXTURES / f"{fixture}.json.gz"))
    return out


def passed(evals):
    return [e for e in evals if e.passed]


def test_boards_produce_options(evals):
    # tech-company boards: only a handful of ops / strategy roles (0–1 years) pass
    # >= 4 since Nov 9: a "Housing" title alone no longer passes (EliseAI's Growth PM is product work)
    assert len(evals) > 700 and len(passed(evals)) >= 4
    titles = {e.posting.title for e in passed(evals)}
    # roles in your fields survive the field filter (EliseAI is a housing company)
    assert {"Deployment Strategist", "Revenue Operations Associate"} <= titles, titles
    # ...but its SDR pays a $60-70K base (OTE is commission): below your minimum (Nov 9)
    assert "Sales Development Representative | Housing" not in titles
    # ... and none of the passes are product, design, finance, HR or pure engineering titles
    assert not [t for t in titles if re.search(
        r"\b(product manager|product designer|program manager|financial analyst|recruit\w*|engineer)\b", t, re.I)]


def test_every_drop_explains_itself(evals):
    for e in evals:
        if not e.passed:
            assert all(f.gate and f.matched for f in e.failures), e.posting.title


def test_passed_meet_employment_location_experience(evals, criteria):
    allow = set(criteria.get("employment_type", "allow")) | {"unknown"}
    cap = criteria.get("experience", "drop_at_or_above")
    for e in passed(evals):
        p = e.posting
        assert p.employment_type in allow, p.title
        assert p.location.offices and not p.location.fully_remote, p.title
        assert all(a.years < cap for a in p.experience.asks), p.title


def test_passed_meet_salary_floor(evals, criteria):
    markets = criteria.get("salary", "markets")
    for e in passed(evals):
        c = e.posting.comp
        if c.known:
            market = {"USD": "US", "CAD": "CA"}[c.currency]
            assert c.midpoint_annual >= markets[market]["floor"], e.posting.title
            if c.midpoint_annual < markets[market]["comfortable"]:
                assert "Low base — check incentives" in e.flags
        else:
            assert any(f.startswith("Salary unknown") for f in e.flags)


def test_passed_titles_are_entry_level_and_non_engineering(evals, criteria):
    tech = criteria.get("technical")
    keep = tech["keep_titles"]
    for e in passed(evals):
        t = e.posting.title
        assert not re.search(r"\b(?:senior|sr\.?|staff|principal|director|vp|head of)\b", t, re.I), t
        if re.search(r"\bengineer", t, re.I):
            assert find_terms(t, keep, first_only=True), t


def test_passed_us_roles_have_no_citizenship_or_clearance(evals, criteria):
    terms = criteria.get("work_authorization", "drop_terms")
    for e in passed(evals):
        if e.posting.is_us:
            assert not find_terms(e.posting.description_lines, terms, first_only=True), e.posting.title
            assert e.visa.startswith("OPT year 1")


def test_engineer_titles_dropped_unless_allowed(evals, criteria):
    from src.pipeline.qualifications import is_ai_builder
    keep = criteria.get("technical", "keep_titles")
    eng = [e for e in evals if re.search(r"\b(?:engineer|developer)\b", e.posting.title, re.I)
           and not find_terms(e.posting.title, keep, first_only=True)
           and not is_ai_builder(e.posting.title, criteria)]
    assert eng and all("technical" in {f.gate for f in e.failures} for e in eng)


def test_internships_always_dropped(evals):
    interns = [e for e in evals if re.search(r"\bintern(?:ship)?s?\b", e.posting.title, re.I)]
    assert interns and not any(e.passed for e in interns)

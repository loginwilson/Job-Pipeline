import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"

from src.config import Company, load_criteria  # noqa: E402
from src.pipeline.gates import Gates  # noqa: E402
from src.pipeline.locations import LocationMatcher  # noqa: E402
from src.pipeline.normalize import normalize_posting  # noqa: E402
from src.sources.base import CompRange, RawPosting  # noqa: E402


@pytest.fixture(scope="session")
def full_criteria():
    """criteria.yaml exactly as configured (including the field-fit rule)."""
    return load_criteria()


@pytest.fixture(scope="session")
def criteria():
    """criteria.yaml without the field-fit and no-years mid-level rules, so each other
    rule's tests can use any title; those two rules are tested with full_criteria."""
    import copy
    from src.config import Criteria
    data = copy.deepcopy(load_criteria().data)
    data.pop("field_fit", None)
    data.pop("description_min_words", None)
    data["salary"].pop("drop_unknown", None)          # tested on its own (test_unknown_salary_rule)
    data["experience"].pop("no_years_mid_level_titles", None)
    return Criteria(data)


@pytest.fixture(scope="session")
def matcher(criteria):
    return LocationMatcher(criteria)


@pytest.fixture(scope="session")
def gates(criteria):
    return Gates(criteria)


@pytest.fixture
def make(criteria, matcher):
    """Build a normalized Posting from a few fields."""
    def _make(title="Deployment Strategist", lines=None, locations=("New York, NY",), workplace="hybrid",
              employment="Full-time", team=None, comp=None, sectors=(), country=None, **company_kw):
        raw = RawPosting(platform="test", slug="acme", native_id=title, title=title, url="https://x/y",
                         team=team, locations=list(locations), workplace_type=workplace,
                         employment_type=employment, country=country,
                         description_lines=list(lines or []), comp_ranges=list(comp or []))
        company = Company(name="Acme", platform="test", slug="acme", sectors=list(sectors), **company_kw)
        return normalize_posting(raw, company, criteria, matcher)
    return _make


@pytest.fixture
def evaluate(make, gates):
    def _eval(**kw):
        return gates.evaluate(make(**kw))
    return _eval


PAY_OK = "The salary range for this position is $150,000 - $170,000/year."

__all__ = ["CompRange", "FIXTURES", "PAY_OK"]

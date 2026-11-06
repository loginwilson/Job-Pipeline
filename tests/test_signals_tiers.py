"""Signals + tiers: necessities decide the tier, preferences only reorder."""
import pytest
from conftest import PAY_OK

from src.pipeline.signals import SignalScorer
from src.pipeline.tiers import assign_tier

REQS = ["## Requirements", "- Experience conducting market research", "- Active real estate license"]


@pytest.fixture
def score(evaluate, criteria):
    scorer = SignalScorer(criteria)

    def _score(**kw):
        ev = evaluate(**kw)
        assert ev.passed, ev.failures
        sig = scorer.score(ev)
        return ev, sig, assign_tier(ev, sig, criteria)
    return _score


def test_preference_never_changes_tier(score):
    _, s_sales, t_sales = score(title="Account Executive", lines=[PAY_OK] + REQS)
    _, s_dep, t_dep = score(title="Deployment Strategist", lines=[PAY_OK] + REQS)
    assert s_sales.role_type == "Sales" and s_dep.role_type == "Deployment"
    assert t_sales.fit_score == t_dep.fit_score and t_sales.tier == t_dep.tier
    assert t_sales.sort_score < t_dep.sort_score            # Sales ranked below an equal match


def test_analyst_titles_sort_lower_only(score):
    _, _, t_an = score(title="Market Analyst", lines=[PAY_OK] + REQS)
    _, _, t_ass = score(title="Market Associate", lines=[PAY_OK] + REQS)
    assert t_an.fit_score == t_ass.fit_score and t_an.tier == t_ass.tier
    assert t_an.sort_score == t_ass.sort_score - 10


def test_strong_requires_no_required_gaps(score):
    _, _, t = score(lines=[PAY_OK] + REQS)
    assert t.tier == "Strong"
    _, _, t_gap = score(lines=[PAY_OK] + REQS + ["- Python required"])
    assert t_gap.tier != "Strong"


def test_necessities_move_the_score(score):
    _, _, nyc = score(lines=[PAY_OK] + REQS)
    _, _, tex = score(locations=["Austin, TX"], lines=[PAY_OK] + REQS)
    assert nyc.fit_score > tex.fit_score                      # location tier is a necessity signal
    _, _, exp2 = score(lines=[PAY_OK] + REQS + ["- 2+ years of experience in finance"])
    assert exp2.fit_score < nyc.fit_score


def test_anchor_never_stored(score):
    _, _, t = score(lines=[PAY_OK, "## Requirements", "- Ability to travel", "- Fluent Korean"], anchor=True)
    assert t.tier in ("Strong", "Worth a look")


def test_department_ignored_for_role_type(score):
    ev, sig, _ = score(title="Deployment Strategist", team="Business Development", lines=[PAY_OK])
    assert sig.role_type == "Deployment" and "Sales-adjacent" not in ev.flags


def test_sales_adjacent_from_duties(score):
    ev, sig, _ = score(title="Growth Associate",
                       lines=[PAY_OK, "You will own a quota, do cold outreach and prospecting."])
    assert sig.sales_adjacent and "Sales-adjacent" in ev.flags


def test_role_type_labels(score):
    assert score(title="Product Designer", lines=[PAY_OK])[1].role_type == "Product Design"
    assert score(title="Associate Product Manager", lines=[PAY_OK])[1].role_type == "Product"
    assert score(title="Acquisitions Analyst", lines=[PAY_OK])[1].role_type == "Real Estate Finance"
    assert score(title="Strategy and Operations Associate", lines=[PAY_OK])[1].role_type == "Strategy & Ops"

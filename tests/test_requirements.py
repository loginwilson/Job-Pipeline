import pytest

from src.pipeline.requirements import RequirementsMatcher


@pytest.fixture(scope="module")
def rm(criteria):
    return RequirementsMatcher(criteria)


def cls(rm, text, section="required"):
    return rm.classify(text, section)


def test_synonyms_met(rm):
    assert cls(rm, "Active real estate license").cls == "met"
    assert cls(rm, "Experience conducting market research").cls == "met"
    assert cls(rm, "Comfortable with AI tools and LLMs").cls == "met"
    assert cls(rm, "Bachelor's degree in Finance, Real Estate or related field").cls == "met"
    assert cls(rm, "Minimum 3.5 GPA").cls == "met"
    assert cls(rm, "Must be graduating in December 2026 or Spring 2027").cls == "met"


def test_stretch_and_gap(rm):
    assert cls(rm, "Proficient in SQL").cls == "stretch"
    assert cls(rm, "Experience with Figma").cls == "stretch"
    assert cls(rm, "Python required").cls == "gap"
    assert cls(rm, "2+ years of experience in operations").cls == "gap"
    assert cls(rm, "MBA required").cls == "gap"


def test_alternatives_best_of(rm):
    it = cls(rm, "Experience with programming, scripting or statistical packages (e.g. Python, R, Matlab, SQL)")
    assert it.cls == "stretch" and it.label == "SQL"
    assert cls(rm, "SQL and Python").cls == "gap"


def test_unclear_half_credit(rm, criteria):
    res = rm.match(["Ability to travel 25% of the time"], [])
    assert res.items[0].cls == "unclear"
    sc = criteria.get("requirements_match", "scoring")
    assert res.score == pytest.approx(100 * (sc["required_stretch"] / 2) / sc["required_met"])
    assert not res.has_required_gap


def test_hard_gap(rm):
    assert cls(rm, "BS in Computer Science degree required").hard
    assert cls(rm, "Experience writing production code").hard
    assert not cls(rm, "Experience writing production code", "preferred").hard
    assert not cls(rm, "CS degree or equivalent experience").hard


def test_score_and_summary(rm):
    res = rm.match(["Experience conducting market research", "Proficient in SQL", "Python required"],
                   ["Experience with Figma"])
    assert res.summary.startswith("Met 1/4 · Stretch: Figma, SQL · Gap: Python")
    assert res.score == pytest.approx(100 * (10 + 5 + 0 + 2) / 34, abs=0.1)
    assert res.has_required_gap

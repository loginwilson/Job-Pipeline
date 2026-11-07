"""JSearch source: parsing, daily budget + rotation, staleness, discovery from apply links."""
import pytest
from conftest import FIXTURES

import src.run as run_mod
from src.pipeline.gates import Gates
from src.pipeline.locations import LocationMatcher
from src.resolve.discover import board_from_url
from src.sources import load_fixture
from src.sources.jsearch import JSearchSource
from src.store.db import Store

T1, T2 = "2026-11-07T10:00:00+00:00", "2026-11-07T11:00:00+00:00"
CFG = {"enabled": True, "requests_per_day": 3, "keywords": ["associate", "product"],
       "cities": [{"query": "New York, NY", "country": "us"}, {"query": "Toronto, ON", "country": "ca"}],
       "first_fill_window": "month", "window": "3days"}


@pytest.fixture
def payload():
    return load_fixture(FIXTURES / "jsearch_search.json.gz")


def test_parse(payload):
    raws = JSearchSource().parse(payload)
    assert len(raws) == 10
    r = next(r for r in raws if r.comp_ranges)
    assert r.platform == "jsearch" and r.company and r.url.startswith("http") and r.country == "US"
    assert r.comp_ranges[0].min >= 1000 and r.posted_at.startswith("2026-")
    assert r.raw["apply_links"] and r.employment_type


def test_budget_and_rotation(monkeypatch, criteria, payload):
    calls = []

    def fake_search(query, country, window, session=None, key=None):
        calls.append((query, country, window))
        return payload
    monkeypatch.setattr(run_mod, "jsearch_search", fake_search)
    store, gates, matcher = Store(":memory:"), Gates(criteria), LocationMatcher(criteria)
    evals = run_mod.run_jsearch(store, CFG, criteria, gates, matcher, T1)
    assert len(calls) == 3 and all(w == "month" for *_, w in calls)          # first fill, budget of 3
    assert evals and len(evals) == 30
    assert run_mod.run_jsearch(store, CFG, criteria, gates, matcher, T2) is None   # budget spent today
    nxt = "2026-11-08T10:00:00+00:00"
    run_mod.run_jsearch(store, CFG, criteria, gates, matcher, nxt)
    assert calls[3][0] not in {c[0] for c in calls[:3]}                       # the 4th pair comes next
    assert calls[3][2] == "month" and calls[4][2] == "3days"                  # then recent-only
    assert ("product in Toronto, ON", "ca") in {(q, c) for q, c, _ in calls}


def test_close_after_days_unseen(criteria, payload):
    store, gates, matcher = Store(":memory:"), Gates(criteria), LocationMatcher(criteria)
    evals = run_mod.evaluate_raws(JSearchSource().parse(payload), criteria, gates, matcher)
    run_mod.store_evaluations(store, run_mod.Company("JSearch", "jsearch", "search"), evals, T1, criteria,
                              prefix="jsearch:__none__")
    assert store.close_stale("jsearch:", 7, "2026-11-10T10:00:00+00:00") == 0
    assert store.close_stale("jsearch:", 7, "2026-11-15T10:00:00+00:00") > 0


def test_board_from_url():
    assert board_from_url("https://job-boards.greenhouse.io/acme/jobs/1") == ("greenhouse", "acme")
    assert board_from_url("https://jobs.lever.co/acme/abc") == ("lever", "acme")
    assert board_from_url("https://acme.wd5.myworkdayjobs.com/en-US/External/job/X") == \
        ("workday", "acme.wd5/External")
    assert board_from_url("https://www.indeed.com/viewjob?jk=1") is None


def test_discovery_from_apply_link(monkeypatch, criteria, payload):
    store, gates, matcher = Store(":memory:"), Gates(criteria), LocationMatcher(criteria)
    raws = JSearchSource().parse(payload)
    raws[1].raw["apply_links"] = ["https://jobs.lever.co/away/123"]
    raws[2].raw["apply_links"] = ["https://jsfc.wd1.myworkdayjobs.com/Careers/job/NY/x"]
    evals = run_mod.evaluate_raws(raws[1:3], criteria, gates, matcher)
    monkeypatch.setattr(run_mod, "verify_board", lambda platform, slug, session=None: 12)
    monkeypatch.setattr(run_mod, "find_board", lambda *a, **k: None)
    found = run_mod.discover(store, evals, {"enabled": True}, set(), T1)
    assert [(c.platform, c.slug) for c in found] == [("lever", "away"), ("workday", "jsfc.wd1/Careers")]


def test_workday_fixture():
    from src.sources import get_source
    payload = load_fixture(FIXTURES / "workday_costar.json.gz")
    raws = get_source("workday").parse(payload, "costar.wd1/CoStarCareers")
    assert len(raws) == 20
    r = next(r for r in raws if r.raw["has_detail"])
    assert r.url.startswith("https://costar.wd1.myworkdayjobs.com/CoStarCareers/job/")
    assert r.country == "US" and r.employment_type == "Full time" and r.description_lines and r.posted_at


def test_workday_skips_closed():
    from src.sources import get_source
    item = {"title": "Analyst", "externalPath": "/job/x", "locationsText": "New York, NY", "postedOn": "Posted Today"}
    payload = {"pages": [{"jobPostings": [item]}], "details": {"/job/x": {"jobPostingInfo": {"canApply": False}}}}
    assert get_source("workday").parse(payload, "acme.wd1/Careers") == []


def test_verify_workday_board_does_not_crash(monkeypatch):
    """Run #9 regression: checking a discovered Workday site must parse with its real board id."""
    from src.resolve import discover
    from src.sources.workday import WorkdaySource
    pages = load_fixture(FIXTURES / "workday_costar.json.gz")["pages"]
    monkeypatch.setattr(WorkdaySource, "fetch_payload_list", lambda self, slug, session=None: {"pages": pages})
    assert discover.verify_board("workday", "costar.wd1/CoStarCareers") == 20

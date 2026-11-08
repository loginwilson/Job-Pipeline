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
    monkeypatch.setenv("JSEARCH_API_KEY", "test-placeholder")
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


def test_no_key_skips_quietly(monkeypatch, criteria):
    monkeypatch.delenv("JSEARCH_API_KEY", raising=False)
    store = Store(":memory:")
    assert run_mod.run_jsearch(store, CFG, criteria, Gates(criteria), LocationMatcher(criteria), T1) is None
    assert store.board_health() == []          # not recorded as a board error


def test_workday_big_site_reads_only_your_cities(monkeypatch, criteria):
    from src.pipeline.locations import LocationMatcher
    from src.sources import workday
    m = LocationMatcher(criteria)
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locations", "values": [
            {"descriptor": "US-NY-New York", "id": "ny", "count": 30},
            {"descriptor": "Santa Clara, CA", "id": "sc", "count": 5000}]}]}]
    calls = []

    def fake(self, method, url, sess, body=None):
        calls.append(body["appliedFacets"])
        if body["appliedFacets"]:
            n = 30
            posts = [{"title": f"Analyst {i}", "externalPath": f"/job/NY/{i}", "locationsText": "US-NY-New York"}
                     for i in range(body["offset"], min(body["offset"] + 20, n))]
            return {"total": n, "jobPostings": posts}
        return {"total": 5030, "facets": facets,
                "jobPostings": [{"title": "X", "externalPath": f"/job/a/{body['offset'] + i}"} for i in range(20)]}
    monkeypatch.setattr(workday.WorkdaySource, "_request", fake)
    src = workday.WorkdaySource()
    items = src.fetch_list("big.wd1/Careers", place_ok=lambda d: m.match_component(d) is not None, max_pages=3)
    assert len(items) == 30 and not items.truncated and items.filtered_to == 1
    assert calls[-1] == {"locations": ["ny"]}
    capped = src.fetch_list("big.wd1/Careers", max_pages=3)           # no city filter available
    assert capped.truncated and len(capped) == 60


def test_truncated_board_closes_nothing(criteria):
    from src.config import Company
    from src.run import evaluate_raws, store_evaluations
    from src.pipeline.gates import Gates
    from src.pipeline.locations import LocationMatcher
    from src.sources.base import RawPosting
    from src.store.db import Store
    from conftest import PAY_OK
    store, co = Store(":memory:"), Company("Big", "workday", "big.wd1/Careers")
    raw = RawPosting(platform="workday", slug=co.slug, native_id="1", title="Operations Associate", url="u",
                     locations=["New York, NY"], workplace_type="hybrid", employment_type="Full-time",
                     description_lines=[PAY_OK])
    ev = evaluate_raws([raw], criteria, Gates(criteria), LocationMatcher(criteria), company=co)
    store_evaluations(store, co, ev, "2026-11-07T10:00:00+00:00", criteria)
    store_evaluations(store, co, [], "2026-11-07T11:00:00+00:00", criteria, close=False)
    assert store.conn.execute("SELECT closed_at FROM postings").fetchone()[0] is None


def test_stored_search_results_are_rechecked(criteria):
    import json
    from src.config import Company
    from src.pipeline.gates import Gates
    from src.pipeline.locations import LocationMatcher
    from src.run import recheck_search_postings, store_evaluations, evaluate_raws
    from src.sources.jsearch import JSearchSource
    from src.store.db import Store
    job = {"job_id": "x1", "job_title": "Full Time - Sales Associate - Hardware", "employer_name": "Lowes",
           "job_city": "Austin", "job_state": "TX", "job_country": "US", "job_employment_type": "Full-time",
           "job_description": "Help customers on the sales floor.", "job_apply_link": "https://x/1"}
    raw = JSearchSource().parse({"data": [job]})[0]
    store, g, m = Store(":memory:"), Gates(criteria), LocationMatcher(criteria)
    ev = evaluate_raws([raw], criteria, g, m)
    store_evaluations(store, Company("JSearch", "jsearch", "search"), ev, "2026-11-08T00:00:00+00:00", criteria, prefix="jsearch:")
    # pretend it passed under older rules
    store.conn.execute("UPDATE scores SET passed=1"); store.conn.execute("UPDATE postings SET snapshot=?", (json.dumps(raw.raw),))
    assert recheck_search_postings(store, criteria, g, m, "2026-11-08T01:00:00+00:00") == 1
    assert store.conn.execute("SELECT passed FROM scores").fetchone()[0] == 0


def test_pay_in_title_is_read(criteria):
    from conftest import FIXTURES  # noqa
    from test_gates import _p
    p = _p(criteria, "Medical Device Driver - 1st Shift - $22.10/hr", [], pay="")
    assert p.comp.known and p.comp.midpoint_annual < 100000

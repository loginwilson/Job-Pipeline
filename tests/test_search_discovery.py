"""The Muse source, auto-discovery of company boards, and merging duplicates."""
from datetime import datetime, timezone

import pytest
from conftest import FIXTURES, PAY_OK

from src.config import Company
from src.pipeline.gates import Gates
from src.pipeline.locations import LocationMatcher
from src.resolve import discover
from src.run import dedupe_key, evaluate_raws, store_evaluations
from src.sources import load_fixture
from src.sources.base import RawPosting, SourceError
from src.sources.muse import MuseSource, muse_locations
from src.store.db import Store

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
T1, T2 = "2026-11-07T10:00:00+00:00", "2026-11-07T11:00:00+00:00"


def test_muse_locations_cover_every_criteria_city(criteria):
    locs = muse_locations(criteria)
    for want in ("New York, NY", "Manhattan, NY", "Jersey City, NJ", "Chicago, IL", "Miami, FL",
                 "San Francisco, CA", "Toronto, Canada", "Austin, TX", "Vancouver, Canada"):
        assert want in locs


def test_muse_parse_fixture():
    pages = load_fixture(FIXTURES / "muse_entry_pages.json.gz")
    raws = MuseSource({"max_age_days": 60}).parse(pages, now=NOW)
    every = MuseSource({"max_age_days": 100000}).parse(pages, now=NOW)
    assert 0 < len(raws) < len(every) == sum(len(p["results"]) for p in pages)   # old postings skipped
    r = raws[0]
    assert r.platform == "muse" and r.company and r.url.startswith("https://www.themuse.com/jobs/")


def test_muse_remote_only_is_remote():
    page = {"results": [{"id": 1, "name": "Analyst", "contents": "<p>x</p>", "publication_date": "2026-11-01T00:00:00Z",
                         "locations": [{"name": "Flexible / Remote"}], "company": {"name": "Acme", "short_name": "acme"},
                         "refs": {"landing_page": "https://www.themuse.com/jobs/acme/analyst"}}]}
    r = MuseSource({}).parse(page, now=NOW)[0]
    assert r.workplace_type == "remote"


def test_candidate_slugs():
    assert discover.candidate_slugs("The TJX Companies, Inc.")[:2] == ["thetjxcompanies", "the-tjx-companies"]
    assert "flexport" in discover.candidate_slugs("Flexport", "flexport")


def test_find_board_requires_matching_title(monkeypatch):
    boards = {("greenhouse", "acme"): {"jobs": [{"id": 1, "title": "Operations Associate", "absolute_url": "u",
                                               "location": {"name": "New York, NY"}, "content": ""}]},
              ("lever", "other"): [{"id": "x", "text": "Software Engineer", "hostedUrl": "u", "categories": {}}]}

    def fake_fetch(self, slug, session=None):
        if (self.platform, slug) in boards:
            return boards[(self.platform, slug)]
        raise SourceError("404")
    monkeypatch.setattr("src.sources.base.Source.fetch_payload", fake_fetch)
    hit = discover.find_board("Acme Inc", ["Operations Associate"])
    assert hit and (hit.platform, hit.slug) == ("greenhouse", "acme")
    assert discover.find_board("Other", ["Customer Success Associate"]) is None   # board exists, job doesn't


@pytest.fixture
def tools(criteria):
    return Gates(criteria), LocationMatcher(criteria)


def raw(platform, slug, nid, company=None, title="Operations Associate"):
    return RawPosting(platform=platform, slug=slug, native_id=nid, title=title, url=f"https://x/{nid}",
                      locations=["New York, NY"], workplace_type="hybrid", employment_type="Full-time",
                      description_lines=[PAY_OK], company=company)


def test_board_copy_absorbs_search_copy_and_keeps_decisions(criteria, tools):
    gates, matcher = tools
    store = Store(":memory:")
    muse_ev = evaluate_raws([raw("muse", "acme", "9", company="Acme")], criteria, gates, matcher)
    store_evaluations(store, Company("The Muse", "muse", "search"), muse_ev, T1, criteria, prefix="muse:")
    muse_id = muse_ev[0].posting.posting_id
    store.save_owner_state(muse_id, {"Decision": "Apply", "Interest": "4", "Status": "", "Notes": "seen on Muse"}, T1)

    acme = Company("Acme", "greenhouse", "acme")
    board_ev = evaluate_raws([raw("greenhouse", "acme", "1")], criteria, gates, matcher, company=acme)
    assert dedupe_key(board_ev[0]) == dedupe_key(muse_ev[0])
    store_evaluations(store, acme, board_ev, T2, criteria)
    ids = [r["posting_id"] for r in store.pipeline_rows()]
    board_id = board_ev[0].posting.posting_id
    assert ids == [board_id] and store.is_merged(muse_id)
    st = store.owner_states()[board_id]
    assert (st["decision"], st["interest"], st["notes"]) == ("Apply", "4", "seen on Muse")
    first = store.conn.execute("SELECT first_seen FROM postings WHERE posting_id=?", (board_id,)).fetchone()[0]
    assert first == T1                                     # keeps the earlier first-seen date

    # a later Muse sweep doesn't bring the copy back
    store_evaluations(store, Company("The Muse", "muse", "search"), muse_ev, T2, criteria, prefix="muse:")
    assert [r["posting_id"] for r in store.pipeline_rows()] == [board_id]


def test_close_missing_only_touches_its_own_board(criteria, tools):
    gates, matcher = tools
    store = Store(":memory:")
    a, b = Company("A Co", "lever", "aco"), Company("B Co", "lever", "bco")
    store_evaluations(store, a, evaluate_raws([raw("lever", "aco", "1")], criteria, gates, matcher, a), T1, criteria)
    store_evaluations(store, b, evaluate_raws([raw("lever", "bco", "2")], criteria, gates, matcher, b), T1, criteria)
    store_evaluations(store, a, [], T2, criteria)          # A's posting disappears
    closed = dict(store.conn.execute("SELECT posting_id, closed_at FROM postings").fetchall())
    assert closed["lever:aco:1"] == T2 and closed["lever:bco:2"] is None

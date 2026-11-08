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


def test_search_copy_dropped_when_watched_board_lacks_it(criteria, tools):
    gates, matcher = tools
    store = Store(":memory:")
    muse_ev = evaluate_raws([raw("muse", "acme", "9", company="Acme", title="Strategy Associate")],
                            criteria, gates, matcher)
    c = store_evaluations(store, Company("The Muse", "muse", "search"), muse_ev, T1, criteria, prefix="muse:",
                          board_keys=set(), board_companies={"acme"})
    assert store.pipeline_rows() == []          # we watch Acme's board and it doesn't list this job


def test_same_board_duplicates_show_once(criteria, tools):
    gates, matcher = tools
    store = Store(":memory:")
    acme = Company("Acme", "greenhouse", "acme")
    raws = [raw("greenhouse", "acme", str(i), title="Rotational Development Program") for i in (1, 2, 3)]
    evals = evaluate_raws(raws, criteria, gates, matcher, company=acme)
    c = store_evaluations(store, acme, evals, T1, criteria)
    assert [r["posting_id"] for r in store.pipeline_rows()] == ["greenhouse:acme:1"]
    assert c["reasons"]["duplicate (same board, same title + city)"] == 2
    store_evaluations(store, acme, evals, T2, criteria)          # still one row, nothing closed
    assert [r["posting_id"] for r in store.pipeline_rows()] == ["greenhouse:acme:1"]


def test_quiet_board_detection():
    store = Store(":memory:")
    store.board_ok("Acme", "greenhouse", 10, T1)
    store.note_board_passes("Acme", 0, T1)
    assert not store.board_is_quiet("Acme", 3, T2)
    assert store.board_is_quiet("Acme", 3, "2026-11-11T11:00:00+00:00")
    store.note_board_passes("Acme", 2, "2026-11-11T11:00:00+00:00")
    assert not store.board_is_quiet("Acme", 3, "2026-11-12T11:00:00+00:00")


def test_hn_posts_and_discovery(monkeypatch, criteria):
    import src.run as run_mod
    from src.sources import hn
    item = load_fixture(FIXTURES / "hn_whoishiring.json.gz")
    posts = hn.posts(item)
    assert len(posts) == 80 and all(p["company"] for p in posts)
    fake = {"children": [
        {"text": "Acme Robotics | Operations Associate | NYC | https://jobs.ashbyhq.com/acme-robotics/123"},
        {"text": "Overjoyed to announce our big round, we are hiring in New York City! https://jobs.lever.co/coolco"},
        {"text": "Faraway Inc | Analyst | Berlin only | https://boards.greenhouse.io/faraway"},
    ]}
    monkeypatch.setattr(hn, "recent_threads", lambda n, s: ["1"])
    monkeypatch.setattr(hn, "thread", lambda i, s: fake)
    monkeypatch.setattr(run_mod, "verify_board", lambda platform, slug, session=None: 5)
    store = Store(":memory:")
    found = run_mod.discover_from_hn(store, {"threads": 1}, set(), LocationMatcher(criteria), T1)
    assert sorted((c.name, c.platform, c.slug) for c in found) == [
        ("Acme Robotics", "ashby", "acme-robotics"), ("Coolco", "lever", "coolco")]   # Berlin-only post skipped


def test_newgrad_list_discovery(monkeypatch, criteria):
    import src.run as run_mod
    from src.sources import newgrad_lists
    data = load_fixture(FIXTURES / "newgrad_list.json.gz")
    assert 0 < len(newgrad_lists.open_listings(data)) < len(data)          # closed listings skipped
    fake = [
        {"company_name": "Acme", "title": "Analyst", "active": True, "locations": ["New York, NY"],
         "url": "https://job-boards.greenhouse.io/acme/jobs/1"},
        {"company_name": "Faraway", "title": "Analyst", "active": True, "locations": ["Berlin, Germany"],
         "url": "https://jobs.lever.co/faraway/2"},
        {"company_name": "Gone", "title": "Analyst", "active": False, "locations": ["Chicago, IL"],
         "url": "https://jobs.ashbyhq.com/gone/3"},
        {"company_name": "Own Site", "title": "Analyst", "active": True, "locations": ["Chicago, IL"],
         "url": "https://careers.ownsite.com/jobs/4"},
    ]
    monkeypatch.setattr(newgrad_lists, "fetch", lambda url, s=None: fake)
    monkeypatch.setattr(run_mod, "verify_board", lambda platform, slug, session=None: 3)
    store = Store(":memory:")
    found = run_mod.discover_from_newgrad_lists(store, {"urls": ["x"]}, set(), LocationMatcher(criteria), T1)
    assert [(c.name, c.platform, c.slug) for c in found] == [("Acme", "greenhouse", "acme")]
    assert store.conn.execute("SELECT seen FROM link_domains WHERE domain='careers.ownsite.com'").fetchone()[0] == 1


def test_greenhouse_behind_company_career_page():
    class Sess:
        def __init__(self): self.urls = []
        def get(self, url, **kw):
            self.urls.append(url)
            return type("R", (), {"status_code": 200 if "/boards/optiverus/jobs/8451763002" in url else 404})()
    s = Sess()
    assert discover.greenhouse_behind("https://www.optiver.com/join-us/jobs/8451763002/?gh_jid=8451763002", "Optiver", s) == "optiverus"
    assert discover.greenhouse_behind("https://www.optiver.com/join-us/jobs/1/", "Optiver", s) is None     # no job id
    assert len(s.urls) <= 10


def test_same_board_under_two_names_is_watched_once():
    store = Store(":memory:")
    store.add_discovered("JPMorganChase", "oracle", "jpmc.fa.oraclecloud.com/CX_1001", "Hacker News", "", T1)
    store.add_discovered("JP Morgan Chase", "oracle", "jpmc.fa.oraclecloud.com/CX_1001", "New-grad lists", "", T2)
    assert [r["company"] for r in store.discovered_boards()] == ["JPMorganChase"]


def test_old_search_copies_without_full_text_are_rechecked_by_title(criteria):
    """JSearch rows stored before full copies were kept used to skip the re-check forever."""
    from src.run import recheck_search_postings
    from src.store.db import Store
    from src.pipeline.gates import Gates
    from src.pipeline.locations import LocationMatcher
    from src.config import load_criteria
    full = load_criteria()
    store = Store(":memory:")
    ts = "2026-11-08T19:00:00+00:00"
    for pid, title in (("jsearch:lowes:1", "Full Time - Sales Associate - Building Materials"),
                       ("jsearch:acme:2", "Business Operations Associate")):
        store.upsert_posting(pid, "Co", title, "https://x", None, {"apply_links": []}, ts)
        store.save_score(pid, True, [], [], {}, ts)
    store.commit()
    recheck_search_postings(store, full, Gates(full), LocationMatcher(full), ts)
    passed = {r["posting_id"]: r["passed"] for r in store.conn.execute("SELECT posting_id, passed FROM scores")}
    assert passed == {"jsearch:lowes:1": 0, "jsearch:acme:2": 1}

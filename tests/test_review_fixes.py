"""Nov 9 operational review: fixes that keep jobs from being lost or merged by mistake."""
import json

from src.output.sheet import _same_job_key
from src.store.db import Store

TS = "2026-11-09T12:00:00+00:00"


def _add(store, pid, gate=None, passed=False, closed=False):
    store.upsert_posting(pid, "Co", "Role", "http://x", None, {"job": {"t": 1}}, TS, dedupe_key=pid)
    store.save_score(pid, passed, [{"gate": gate, "matched": "m"}] if gate else [], [], {}, TS)
    if closed:
        store.conn.execute("UPDATE postings SET closed_at=? WHERE posting_id=?", (TS, pid))


def _snap(store, pid):
    return store.conn.execute("SELECT snapshot FROM postings WHERE posting_id=?", (pid,)).fetchone()["snapshot"]


def test_compact_keeps_text_of_job_site_near_misses():
    s = Store(":memory:")
    _add(s, "jsearch:search:1", "salary")             # near miss from a job site: text kept for the re-check
    _add(s, "adzuna:search:2", "experience")
    _add(s, "jsearch:search:3", "field_fit")          # wrong field: never coming back
    _add(s, "jsearch:search:4", "salary", closed=True)
    _add(s, "ashby:co:5", "salary")                   # company boards are re-read every poll
    _add(s, "ashby:co:6", passed=True)
    _add(s, "ashby:co:7", "field_fit")
    s.conn.execute("INSERT INTO owner_state(posting_id, notes) VALUES ('ashby:co:7', 'asked a friend')")
    s.compact()
    assert _snap(s, "jsearch:search:1") and _snap(s, "adzuna:search:2")
    assert _snap(s, "jsearch:search:3") is None and _snap(s, "jsearch:search:4") is None
    assert _snap(s, "ashby:co:5") is None
    assert _snap(s, "ashby:co:6") and _snap(s, "ashby:co:7")      # passing, and one you wrote a note on
    assert json.loads(_snap(s, "jsearch:search:1"))["job"]


def test_board_first_empty_only_once():
    s = Store(":memory:")
    assert not s.board_first_empty("Co", 0)           # never seen: an empty board is just empty
    s.board_ok("Co", "ashby", 12, TS)
    assert s.board_first_empty("Co", 0)               # had 12, now none: don't close yet
    s.board_ok("Co", "ashby", 0, TS)
    assert not s.board_first_empty("Co", 0)           # empty twice: believe it
    assert not s.board_first_empty("Co", 5)


def test_same_job_key_keeps_teams_apart_and_cities_together():
    k = lambda role, loc="New York, NY (tier 1)": _same_job_key({"Company": "Acme", "Role": role, "Location": loc})
    assert k("Chief of Staff - Finance") != k("Chief of Staff - Product")
    assert k("Operations Associate - New York") == k("Operations Associate")
    assert k("Operations Associate | Remote") == k("Operations Associate")
    assert k("Operations Associate - Austin", "Austin, TX (tier 3)") == k("Operations Associate")
    assert k("Strategy & Operations, Marketplace") != k("Strategy & Operations, Payments")


# --- Nov 9, 3 PM read of the new To review rows ---------------------------------------------
def _ev(full_criteria, title, lines, company="Acme", platform="ashby", locations=("New York, NY",)):
    from src.config import Company
    from src.pipeline.gates import Gates
    from src.pipeline.locations import LocationMatcher
    from src.pipeline.normalize import normalize_posting
    from src.sources.base import RawPosting
    raw = RawPosting(platform=platform, slug="x", native_id=title, title=title, url="https://x", team=None,
                     locations=list(locations), workplace_type="in_office", employment_type="Full-time",
                     country=None, description_lines=list(lines), comp_ranges=[])
    return Gates(full_criteria).evaluate(
        normalize_posting(raw, Company(company, platform, "x"), full_criteria, LocationMatcher(full_criteria)))


BODY = ["Acme builds AI agents for enterprises.", "About the role", "You will work with customers every day.",
        "Requirements", "0-1 years of experience. Recent graduates welcome.",
        "Strong communication skills, curiosity, ownership and the drive to learn fast in a startup with many moving parts."]
PAY = "The salary range for this position is $110,000 - $130,000/year."


def test_posting_that_calls_itself_an_internship_drops(full_criteria):
    ev = _ev(full_criteria, "GTM Strategy & Operations", BODY + [PAY, "During your internship you will be part of SAP Next Gen."])
    assert {f.gate for f in ev.failures} == {"employment_type"}
    assert _ev(full_criteria, "SAPiXp - GTM Strategy & Operations", BODY + [PAY]).failures[0].gate == "employment_type"
    # mentioning past internships is not being one (FSH, Instawork)
    assert _ev(full_criteria, "Operations Associate", BODY + [PAY, "An internal tool you made at an internship.",
                                                             "Previous internships or relevant project work."]).passed


def test_track_record_of_owning_is_a_senior_ask(full_criteria):
    ev = _ev(full_criteria, "Strategy & Operations", BODY[:4] + [
        "Track record of owning operational outcomes end-to-end across strategy, operations, or consulting roles.",
        BODY[5], PAY])
    assert ev.failures and ev.failures[0].gate == "experience"


def test_devrel_behind_a_gtm_engineer_title_is_marketing(full_criteria):
    ev = _ev(full_criteria, "GTM Engineer", BODY + [PAY,
             "Make this company a known name through events, community, and authentic technical evangelism.",
             "1-3+ years in DevRel, community building, growth, or developer-facing GTM roles."])
    assert ev.failures and ev.failures[0].gate == "field"
    assert _ev(full_criteria, "GTM Engineer", BODY + [PAY]).passed


def test_ote_only_pay_is_judged_like_no_pay(full_criteria):
    body = ["Kith is a lifestyle brand with stores around the world.", "About the role",
            "You will support daily operations for the team.", "Requirements", "0-1 years of experience.",
            "Strong communication skills, curiosity, ownership and the drive to learn fast with many moving parts.",
            "OTE: $60,000 - $70,000 per year including commission."]
    kith = _ev(full_criteria, "Operations Associate", body, company="Kith Kids", platform="workday",
               locations=("Vancouver, BC",))
    assert kith.posting.comp.ote_only
    assert kith.failures and kith.failures[0].gate == "salary"


def test_required_stem_degree_drops_even_with_business_operations_on_the_line(full_criteria):
    line = ("A STEM degree and 1–3 years in operations, business operations, automation, solutions engineering "
            "or a similar hands-on role, ideally at an early-stage startup")
    ev = _ev(full_criteria, "Founder's Associate", BODY[:4] + [line, BODY[5], PAY])
    assert ev.failures and ev.failures[0].gate == "credentials"
    ok = _ev(full_criteria, "Founder's Associate", BODY[:4] + [
        "Bachelor's degree in business, economics or a STEM field and 0-1 years in business operations.", BODY[5], PAY])
    assert ok.passed, ok.failures


def test_catch_all_posting_is_not_an_opening(full_criteria):
    ev = _ev(full_criteria, "Generalist", BODY + [PAY,
             "This role is for candidates who think they'd be exceptional members of our team but don’t see a "
             "specific role that matches their skillsets. Let us know what you'd like to work on!"])
    assert ev.failures and ev.failures[0].gate == "not_a_real_opening"
    assert _ev(full_criteria, "Generalist", BODY + [PAY]).passed


def test_job_site_copy_under_a_shorter_company_name_is_a_duplicate():
    from src.output.sheet import _dedupe

    def row(co, role, loc, pid, interest=""):
        return {"cells": {"Company": co, "Role": role, "Location": loc, "Interest": interest, "Status": "",
                          "Notes": "", "Next step": "", "Decision": "Pending"},
                "closed": False, "tier": 1, "posting_id": pid, "first_seen": "2026-11-09"}
    rows = [row("Finch", "Strategy & Operations, Demands", "NYC (tier 1) · in office", "jobspipe::1"),
            row("Finch Legal", "Strategy & Operations, Demands", "NYC (tier 1) · in office", "ashby:f:2", "3"),
            row("Finch Legal", "Revenue Operations", "NYC (tier 1) · in office", "ashby:f:3"),
            row("Apple", "Operations Associate", "NYC (tier 1)", "ashby:a:4"),
            row("Apple Leisure Group", "Operations Associate", "Chicago (tier 2)", "ashby:alg:5")]
    out = [r["posting_id"] for r in _dedupe(rows)]
    assert "jobspipe::1" not in out and "ashby:f:2" in out and "ashby:f:3" in out
    assert "ashby:a:4" in out and "ashby:alg:5" in out      # different city: not merged


def test_track_record_of_owning_problems_is_not_senior(full_criteria):
    ev = _ev(full_criteria, "Associate, Business Operations & Analytics", BODY[:4] + [
        "1+ years of experience at a consulting firm, investment bank, or high-growth technology company.",
        "Track record of owning ambiguous problems and delivering impact.", BODY[5], PAY])
    assert ev.passed, ev.failures


def test_reposted_job_keeps_your_score():
    s = Store(":memory:")
    for pid in ("ashby:jerry.ai:old", "ashby:jerry.ai:new", "ashby:jerry.ai:other"):
        title = "Senior Associate, Business Operations & Analytics" if pid.endswith("other") else \
            "Associate, Business Operations & Analytics"
        s.upsert_posting(pid, "Jerry", title, "u", None, None, TS, dedupe_key=pid)
        s.save_score(pid, True, [], [], {"location": "NYC (tier 1) · remote"}, TS)
    s.conn.execute("INSERT INTO owner_state(posting_id, interest, notes) VALUES ('ashby:jerry.ai:old', '3', 'liked it')")
    s.conn.execute("UPDATE postings SET closed_at=? WHERE posting_id='ashby:jerry.ai:old'", (TS,))
    assert s.carry_reposts(TS) == 1
    st = s.conn.execute("SELECT interest, notes FROM owner_state WHERE posting_id='ashby:jerry.ai:new'").fetchone()
    assert (st["interest"], st["notes"]) == ("3", "liked it")
    assert s.is_merged("ashby:jerry.ai:old") and not s.is_merged("ashby:jerry.ai:new")
    assert s.conn.execute("SELECT 1 FROM owner_state WHERE posting_id='ashby:jerry.ai:other'").fetchone() is None
    assert s.carry_reposts(TS) == 0                       # nothing left to move

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

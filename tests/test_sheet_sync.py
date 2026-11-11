"""Sheet: To review (score), Apply (track), Passed (live view), Contacts; one write per run."""
import copy

import pytest
from conftest import FIXTURES

from src.config import Company
from src.output.sheet import (ALL_TABS, APPLY_HEADERS as AH, HEADERS as H, ID_COL, JOBS, PICKS, FakeBackend,
                              sync_all)
from src.run import evaluate_company, store_evaluations
from src.sources import load_fixture
from src.store.db import Store

REVIEW, APPLICATIONS = JOBS, PICKS
PASSED = "__view: passed"          # Apply and Passed are live formula views of To review
T1, T2 = "2026-11-05T10:17:00+00:00", "2026-11-05T11:17:00+00:00"
T_LATER = "2026-11-16T11:17:00+00:00"        # 11 days after T1
# every tab's rows come back in one column order U (To review's columns, then Apply's extras)
U = H + [h for h in AH if h not in H]
DEC, INT, STATUS, APPLIED, FLAGS, ROLE, NOTES = (U.index(x) for x in
    ("Interest", "Interest", "Status", "Applied on", "Flags", "Role", "Notes"))


@pytest.fixture(scope="module")
def board():
    return Company(name="Example Co", platform="lever", slug="example")


@pytest.fixture(scope="module")
def payload():
    return load_fixture(FIXTURES / "lever_palantir.json.gz")


@pytest.fixture(scope="module")
def evals(board, payload, criteria):
    return evaluate_company(board, payload, criteria)


def run(store, sheet, board, evals, criteria, ts):
    store_evaluations(store, board, evals, ts, criteria)
    store.commit()
    return sync_all(sheet, store, criteria, ts)


def tab(sheet, name):
    if name in (PICKS, PASSED):                  # live formula views: computed here as the formula does
        jobs = tab(sheet, JOBS)
        if name == PASSED:
            return [r for r in jobs if r[INT] == "Pass"]
        picks = [r for r in jobs if r[INT].replace(".0", "").isdigit() and int(float(r[INT])) >= 1]
        ended = {"Rejected", "Withdrawn", "No response"}
        return sorted(picks, key=lambda r: (r[STATUS] in ended, -int(float(r[INT]))))
    rows = sheet.read([name])[name]
    if not rows:
        return []
    head = rows[0]
    return [[dict(zip(head, r + [""] * (len(head) - len(r)))).get(h, "") for h in U] for r in rows[1:]]


def row_index(sheet, name, pid):
    return 1 + [r[U.index("Posting ID")] for r in tab(sheet, name)].index(pid)


def put(sheet, name, pid, header, value):
    """Type a value into the row for this job, in the named column of that tab."""
    head = sheet.read([name])[name][0]
    sheet.set_cell(name, row_index(sheet, name, pid), head.index(header), value)


def test_new_postings_land_in_review(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    info = run(store, sheet, board, evals, criteria, T1)
    passed = sum(e.passed for e in evals) - 1          # Deployment Strategist NYC + Chicago shows once
    assert info["tabs"] == {"To review": passed, "Apply": 0, "Passed": 0}
    assert sheet.read([REVIEW])[REVIEW][0] == H
    assert all(r[DEC] == "" and "New" in r[FLAGS] for r in tab(sheet, REVIEW))
    assert set(sheet.tabs) == set(ALL_TABS) and len(sheet.calls) == 1       # one write per run
    assert [t for t in sorted(sheet.tabs, key=lambda t: sheet.tabs[t]["index"])][:4] == \
        ["To review", "Apply", "Passed", "Contacts"]
    assert sheet.read([PICKS])[PICKS][0] == AH and sheet.read([PICKS])[PICKS][1][0].startswith("=IFERROR(LET(")
    assert sheet.tabs["Passed"]["grid"][0][0]["formulaValue"].startswith("={'To review'!A1:")   # a live view


def test_scores_go_to_apply_and_you_track_there(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    ids = [r[ID_COL] for r in tab(sheet, REVIEW)]
    a, b, c, d = ids[:4]
    put(sheet, REVIEW, a, "Interest", "5")
    put(sheet, REVIEW, b, "Interest", "Pass")
    put(sheet, REVIEW, c, "Interest", "3")
    put(sheet, REVIEW, d, "Interest", "4")
    run(store, sheet, board, evals, criteria, T2)
    assert [r[ID_COL] for r in tab(sheet, APPLICATIONS)] == [a, d, c]          # highest Interest first
    assert [r[ID_COL] for r in tab(sheet, PASSED)] == [b]
    # you track on Apply
    put(sheet, REVIEW, a, "Notes", "Coffee chat")
    put(sheet, REVIEW, c, "Status", "Applied")
    put(sheet, REVIEW, d, "Status", "Rejected")
    put(sheet, REVIEW, a, "Next step", "Email Sam")
    run(store, sheet, board, evals, criteria, T2)
    apps = tab(sheet, APPLICATIONS)
    assert [r[ID_COL] for r in apps] == [a, c, d]                              # finished ones last
    assert apps[0][NOTES] == "Coffee chat" and apps[0][U.index("Next step")] == "Email Sam"
    assert apps[1][APPLIED] == T2[:10]                                         # Applied on stamped automatically
    review = tab(sheet, REVIEW)
    assert {a, b, c, d} <= {r[ID_COL] for r in review} and len(review) == len({r[ID_COL] for r in review})
    run(store, sheet, board, evals, criteria, T2)                              # a third run keeps everything
    assert [r[ID_COL] for r in tab(sheet, APPLICATIONS)] == [a, c, d]
    assert tab(sheet, APPLICATIONS)[0][NOTES] == "Coffee chat"


def test_follow_up_after_ten_days(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    pid = tab(sheet, REVIEW)[0][ID_COL]
    put(sheet, REVIEW, pid, "Interest", "3")
    run(store, sheet, board, evals, criteria, T1)
    put(sheet, REVIEW, pid, "Status", "Applied")
    run(store, sheet, board, evals, criteria, T2)
    assert "Follow up" not in tab(sheet, APPLICATIONS)[0][FLAGS]
    run(store, sheet, board, evals, criteria, T_LATER)
    app = next(r for r in tab(sheet, APPLICATIONS) if r[ID_COL] == pid)
    assert "Follow up" in app[FLAGS] and app[APPLIED] == T2[:10]


def test_owner_values_restored_from_database(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    pid = tab(sheet, REVIEW)[0][ID_COL]
    put(sheet, REVIEW, pid, "Interest", "4")
    run(store, sheet, board, evals, criteria, T2)
    put(sheet, REVIEW, pid, "Status", "Drafting")
    run(store, sheet, board, evals, criteria, T2)
    fresh = FakeBackend()                                    # tabs deleted / new Sheet
    run(store, fresh, board, evals, criteria, T2)
    app = next(r for r in tab(fresh, APPLICATIONS) if r[ID_COL] == pid)
    assert app[INT] == "4" and app[STATUS] == "Drafting"


def test_hyperlinks_and_formatting(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    assert all(f.startswith('=HYPERLINK("https://jobs.lever.co/') for f in sheet.formulas(REVIEW, H.index("Role")))
    t = sheet.tabs[JOBS]
    assert t["frozen"] == 1 and len(t["cf"]) == 9
    kinds = [next(iter(r)) for r in t["other"]]
    assert kinds.count("setDataValidation") == 3 and "setBasicFilter" in kinds
    assert len(sheet.tabs["Apply"]["cf"]) == 8 and sheet.tabs["Passed"]["frozen"] == 1
    run(store, sheet, board, evals, criteria, T2)
    assert len(sheet.tabs[REVIEW]["cf"]) == 9                # rules not duplicated


def test_closed_postings_leave_review_but_stay_in_applications(criteria, board, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evaluate_company(board, payload, criteria), criteria, T1)
    rows = tab(sheet, REVIEW)
    untouched, applied = rows[0][ID_COL], rows[1][ID_COL]
    put(sheet, REVIEW, applied, "Interest", "3")
    gone = {untouched.split(":")[-1], applied.split(":")[-1]}
    shrunk = [p for p in copy.deepcopy(payload) if p["id"] not in gone]       # both close on the board
    run(store, sheet, board, evaluate_company(board, shrunk, criteria), criteria, T2)
    assert untouched not in [r[ID_COL] for r in tab(sheet, REVIEW)]            # not an option anymore
    app = next(r for r in tab(sheet, APPLICATIONS) if r[ID_COL] == applied)
    assert "Closed" in app[FLAGS]                                              # you still see what happened
    run(store, sheet, board, evaluate_company(board, payload, criteria), criteria, T2)   # reopens
    assert untouched in [r[ID_COL] for r in tab(sheet, REVIEW)]


def test_touched_row_never_deleted(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    pid = tab(sheet, REVIEW)[0][ID_COL]
    put(sheet, REVIEW, pid, "Interest", "2")
    run(store, sheet, board, evals, criteria, T1)
    put(sheet, REVIEW, pid, "Notes", "keep me")
    store.conn.execute("UPDATE scores SET passed=0 WHERE posting_id=?", (pid,))   # e.g. criteria tightened
    store.commit()
    sync_all(sheet, store, criteria, T2)
    sync_all(sheet, store, criteria, T2)
    kept = [r for r in tab(sheet, APPLICATIONS) if r[ID_COL] == pid]
    assert kept and kept[0][NOTES] == "keep me"


def test_contacts_link_to_jobs_on_apply_and_old_tabs_go(criteria, board, evals):
    from src.output.sheet import CONTACT_HEADERS
    store, sheet = Store(":memory:"), FakeBackend()
    sheet.ensure_tabs(["Home", "Companies", "Grad programs", "Company jobs", "Before reset"])
    run(store, sheet, board, evals, criteria, T1)
    assert not {"Home", "Companies", "Grad programs", "Company jobs", "Before reset"} & set(sheet.tabs)
    head = sheet.read(["Contacts"])["Contacts"][0]
    assert head[:11] == CONTACT_HEADERS[:11] and head[12] == "Status"           # Follow up / Job link are formulas
    assert "Follow up" in str(head[11]) and "Job link" in str(head[13])
    rules = [r["setDataValidation"]["rule"]["condition"] for r in sheet.tabs["Contacts"]["other"]
             if "rule" in r.get("setDataValidation", {})]
    assert {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "='Apply'!H2:H"}]} in rules   # Company
    assert any(c.get("values") == [{"userEnteredValue": v} for v in ("1", "2", "Hold")] for c in rules)  # Wave
    pid = tab(sheet, REVIEW)[0][ID_COL]
    put(sheet, REVIEW, pid, "Interest", "4")
    for col, v in enumerate(["Example Co", "Sasha", "Ops", "Both"]):
        sheet.set_cell("Contacts", 1, col, v)
    run(store, sheet, board, evals, criteria, T2)
    assert sheet.read(["Contacts"])["Contacts"][1][1] == "Sasha"                 # your row untouched
    assert "COUNTIF(Contacts!A:A, CHOOSECOLS(g, 6))" in sheet.tabs[PICKS]["grid"][1][0]["formulaValue"]


def test_a_job_you_marked_keeps_its_link_when_rules_change(criteria):
    """Meow / Jain Global lost their links after a rule change: marked jobs now stay live."""
    store, sheet = Store(":memory:"), FakeBackend()
    store.upsert_posting("ashby:meow:1", "Meow", "GTM Associate", "https://jobs.example/meow/1", None, {}, T1)
    store.save_score("ashby:meow:1", True, [], [], {}, T1)
    store.commit()
    sync_all(sheet, store, criteria, T1)
    sheet.set_cell(REVIEW, 1, DEC, "Apply")
    sync_all(sheet, store, criteria, T1)
    # a later rule change drops it
    store.save_score("ashby:meow:1", False, [{"gate": "field", "matched": "not one of your fields"}], [], {}, T2)
    store.commit()
    sync_all(sheet, store, criteria, T2)
    raw = next(g for g in sheet.tabs[JOBS]["grid"][1:] if g and g[ID_COL].get("stringValue") == "ashby:meow:1")
    role = raw[H.index("Role")]
    assert "formulaValue" in role and "jobs.example/meow/1" in role["formulaValue"]
    flags = tab(sheet, APPLICATIONS)[0][FLAGS]
    assert "Outside your rules now (field)" in flags and "Closed" not in flags
    assert store.is_tracked("ashby:meow:1")


def test_one_row_per_job_your_edits_and_order_stay(criteria, board, evals):
    """One row per job on To review; your score sticks and your sort order is kept between runs."""
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    ids = [r[ID_COL] for r in tab(sheet, REVIEW)]
    pid = ids[-1]
    put(sheet, REVIEW, pid, "Interest", "5")
    # you sort the whole table (every column, ID included): reverse it
    grid = sheet.tabs[REVIEW]["grid"]
    grid[1:len(ids) + 1] = list(reversed(grid[1:len(ids) + 1]))
    run(store, sheet, board, evals, criteria, T2)
    after = tab(sheet, REVIEW)
    rev = list(reversed(ids))
    assert [r[ID_COL] for r in after] == [i for i in rev if i != pid] + [pid]   # your order; scored last
    assert after[-1][INT] == "5"
    assert [r[ID_COL] for r in tab(sheet, APPLICATIONS)] == [pid]


def test_one_pick_column_pass_and_numbers(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    a, b, c, d = [r[ID_COL] for r in tab(sheet, REVIEW)][:4]
    put(sheet, REVIEW, a, "Interest", "5")
    put(sheet, REVIEW, b, "Interest", "Pass")
    put(sheet, REVIEW, c, "Interest", "Apply")
    put(sheet, REVIEW, d, "Interest", "3.0")                 # a number Sheets stored as 3.0
    run(store, sheet, board, evals, criteria, T2)
    review = {r[ID_COL]: r for r in tab(sheet, REVIEW)}
    assert review[a][INT] == "5" and review[b][INT] == "Pass" and review[c][INT] == "3" and review[d][INT] == "3"
    assert [r[ID_COL] for r in tab(sheet, APPLICATIONS)] == [a, c, d]
    assert [r[ID_COL] for r in tab(sheet, PASSED)] == [b]
    assert sheet.read([REVIEW])[REVIEW][0][:3] == ["Interest", "Status", "Next step"]


def test_other_tabs_and_funnel(criteria, board, evals):
    from src.run import add_stats
    store, sheet = Store(":memory:"), FakeBackend()
    c = store_evaluations(store, board, evals, T1, criteria)
    stats = {}
    add_stats(stats, "Lever", c)
    store.board_ok("Example Co", "lever", len(evals), T1)
    passed = sum(e.passed for e in evals)
    store.record_run(started_at=T1, finished_at=T1, boards_polled=1, postings_seen=len(evals), new=len(evals),
                     passed=passed, errors=0, new_passed=passed, seconds=12.5,
                     coverage='{"boards_polled_by_platform": {"lever": 1}, "jsearch_budget": 6, "open_options": %d}' % passed)
    store.record_run_sources(T1, stats)
    store.commit()
    sync_all(sheet, store, criteria, T1)
    data = sheet.read(ALL_TABS)
    assert len(data["Dropped"]) == 1 + len(evals) - passed
    funnel = data["Funnel"]
    assert funnel[1][0].startswith("Latest run: 2026-11-05 10:17 UTC") and f"{passed} new options" in funnel[1][0]
    src = next(r for r in funnel if r and r[0] == "Lever")
    assert src[1:6] == [str(len(evals)), str(len(evals)), str(passed), str(passed), str(len(evals) - passed)]
    assert "location" in src[6]                                  # top reasons shown
    assert any(r and r[0] == "Company boards watched" for r in funnel)
    hist = funnel[funnel.index(["History"]) + 2]
    assert hist[0] == "2026-11-05 10:17" and hist[5] == str(passed)
    assert data["Board health"][1][-1] == "OK"


def test_first_seen_persists(criteria, board, evals):
    store = Store(":memory:")
    for ts in (T1, T2):
        store_evaluations(store, board, evals, ts, criteria)
    row = store.conn.execute("SELECT first_seen, last_seen, is_new FROM postings LIMIT 1").fetchone()
    assert (row["first_seen"], row["last_seen"], row["is_new"]) == (T1, T2, 0)


def test_dates_without_timezone_do_not_crash(criteria, board, evals):
    """Run #12 regression: a board gave '2026-11-05' (no timezone) as the posted date."""
    store, sheet = Store(":memory:"), FakeBackend()
    store_evaluations(store, board, evals, T1, criteria)
    store.conn.execute("UPDATE postings SET posted_at='2026-11-05' WHERE rowid IN (SELECT rowid FROM postings LIMIT 3)")
    store.conn.execute("UPDATE postings SET posted_at='2026-11-05T09:00:00' WHERE rowid IN "
                       "(SELECT rowid FROM postings LIMIT 3 OFFSET 3)")
    store.commit()
    sync_all(sheet, store, criteria, T1)
    assert tab(sheet, REVIEW)


def test_one_corrupt_posting_never_blocks_the_sheet(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    store_evaluations(store, board, evals, T1, criteria)
    bad = store.pipeline_rows()[0]["posting_id"]
    store.conn.execute("UPDATE scores SET display='{not json' WHERE posting_id=?", (bad,))
    store.commit()
    sync_all(sheet, store, criteria, T1)
    ids = [r[ID_COL] for r in tab(sheet, REVIEW)]
    assert ids and bad not in ids


def test_sort_flags_and_source_names(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    store_evaluations(store, board, evals, T1, criteria)
    store.conn.execute("UPDATE postings SET posted_at='2025-01-01' WHERE rowid IN (SELECT rowid FROM postings LIMIT 2)")
    store.mark_evergreen(180, T1)
    store.commit()
    sync_all(sheet, store, criteria, T1)
    rows = tab(sheet, REVIEW)
    assert sum("Open 6+ months" in r[FLAGS] for r in rows) >= 1          # board's posted date counts
    LOC = H.index("Location")
    tiers = [int(r[LOC].split("tier ")[1][0]) for r in rows if "tier " in r[LOC]]
    assert tiers == sorted(tiers)                                         # same day found → NYC first


def test_unchanged_tabs_are_not_rewritten_and_tabs_shrink(criteria, board, evals):
    """A rewrite makes an open Sheet reload (it froze on big tabs): unchanged tabs are skipped."""
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    run(store, sheet, board, evals, criteria, T1)            # nothing changed
    info = sync_all(sheet, store, criteria, T1)
    assert REVIEW in info["unchanged_tabs"]
    n = len(tab(sheet, REVIEW))
    assert sheet.tabs[REVIEW]["rows"] <= n + 1 + 20           # sized to fit, not to its largest ever


def test_preference_rank_orders_your_fields_first():
    from src.output.sheet import _preference_rank
    flags = ["New", "AI-forward company", "Preferred field: real estate", "Your edge: AI × hospitality, New"]
    assert sorted(flags, key=_preference_rank) == [flags[3], flags[1], flags[2], flags[0]]   # AI first (Nov 9)


def test_company_auto_add_needs_the_company_description(criteria):
    """One job mentioning a hotel isn't enough; the company's own opening lines must (2+ terms)."""
    from types import SimpleNamespace
    from src.run import _note_company_fit
    store = Store(":memory:")
    co = SimpleNamespace(name="X", platform="ashby")
    def ev(company, lines):
        return SimpleNamespace(posting=SimpleNamespace(company=company, description_lines=lines, url="https://x"),
                               flags=[])
    _note_company_fit(store, ev("Pharma", ["We discover medicines.", "## Role", "Book hotels for our events."]), co, T1)
    _note_company_fit(store, ev("Lease AI", ["Lease AI builds AI agents for property management and multifamily "
                                             "apartments, helping landlords and renters."]), co, T1)
    fits = {r["company"]: r["fit"] for r in store.target_companies()}
    assert fits == {"Lease AI": "AI × real estate"}


def test_sorting_without_the_hidden_id_column_keeps_picks_on_the_right_jobs(criteria, board, evals):
    """Nov 8: rows were re-sorted but the hidden Posting ID column stayed put."""
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    grid = sheet.tabs[REVIEW]["grid"]
    rows = [r for r in tab(sheet, REVIEW)]
    # pick the first job (by its own Company + Role), then "sort" every column except the ID
    first_role = rows[0][ROLE]
    sheet.set_cell(REVIEW, 1, INT, "5")
    body = grid[1:len(rows) + 1]
    ids = [r[ID_COL] for r in body]
    body.reverse()
    for r, pid in zip(body, ids):
        r[ID_COL] = pid
    grid[1:len(rows) + 1] = body
    run(store, sheet, board, evals, criteria, T2)
    apps = tab(sheet, APPLICATIONS)
    assert len(apps) == 1 and apps[0][ROLE] == first_role and apps[0][INT] == "5"


def test_unscored_jobs_stay_on_top_and_old_tab_names_are_renamed(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    ids = [r[ID_COL] for r in tab(sheet, REVIEW)]
    sheet.set_cell(REVIEW, 1, INT, "Pass")                     # score the first row
    run(store, sheet, board, evals, criteria, T2)
    after = [r[ID_COL] for r in tab(sheet, REVIEW)]
    assert after[-1] == ids[0] and after[:-1] == ids[1:]       # scored row sinks, the rest keep order
    old = FakeBackend()
    old.ensure_tabs(["Jobs", "My picks"])
    old.rename_tabs({"Jobs": "To review", "My picks": "Apply"})
    assert "To review" in old.tabs and "Jobs" not in old.tabs




def test_apply_and_passed_are_live_views_of_to_review(criteria, board, evals):
    """Nov 9: you type only on To review; Apply (sorted 1-5s) and Passed follow instantly."""
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    f = sheet.tabs[PICKS]["grid"][1][0]["formulaValue"]
    assert sheet.read([PICKS])[PICKS][0][:3] == ["Interest", "Status", "Applied on"]
    assert "FILTER('To review'!A2:T, IFERROR(VALUE('To review'!A2:A), 0)>=1)" in f
    assert "ARRAYFORMULA(VALUE(CHOOSECOLS(f, 1))), FALSE)" in f and "COUNTIF(Contacts!A:A" in f
    assert len(sheet.tabs[PICKS]["grid"]) == 2                    # header + one formula that fills the rest
    assert sheet.tabs["Passed"]["grid"][0][0]["formulaValue"].startswith("={'To review'!A1:")


def test_never_watch_boards_close_untouched_postings(criteria):
    """Nov 9: Marriott / Hilton / Greystar boards were almost all hotel-floor jobs: not read, and
    their untouched postings close (a job you scored stays)."""
    from src.store.db import Store
    store = Store(":memory:")
    for pid, title in (("oracle:m:1", "Property Administrative Specialist"), ("oracle:m:2", "Revenue Analyst")):
        store.upsert_posting(pid, "Marriott International", title, "https://x/" + pid, None, {}, T1)
        store.save_score(pid, True, [], [], {}, T1)
    store.save_owner_state("oracle:m:2", {"Decision": "Apply", "Interest": "4"}, T1)
    store.commit()
    store.conn.execute(
        "UPDATE postings SET closed_at=? WHERE closed_at IS NULL AND lower(trim(company)) = ? AND posting_id NOT IN "
        "(SELECT posting_id FROM owner_state WHERE COALESCE(interest, '') <> '')", (T2, "marriott international"))
    closed = dict(store.conn.execute("SELECT posting_id, closed_at FROM postings").fetchall())
    assert closed["oracle:m:1"] == T2 and closed["oracle:m:2"] is None


def test_no_duplicates_on_the_sheet():
    """Nov 9: ReadyOn / Clay / Finch showed twice (found by two sources)."""
    from src.output.sheet import _dedupe

    def row(pid, company, title, first, interest="", closed=False):
        cells = {"Company": company, "Role": f'=HYPERLINK("https://x/{pid}", "{title}")', "Interest": interest,
                 "Status": "", "Notes": "", "Next step": "", "Decision": "Pending"}
        return {"posting_id": pid, "cells": cells, "closed": closed, "first_seen": first}
    rows = [row("jobspipe:1", "ReadyOn", "Founding Operations Associate", "2026-11-08"),
            row("jobspipe:2", "ReadyOn", "Founding Operations Associate (NYC)", "2026-11-09"),
            row("jobspipe:3", "Clay Labs, Inc.", "GTM Operations - New York", "2026-11-07"),
            row("ashby:clay:9", "Clay", "GTM Operations", "2026-11-09"),
            row("jsearch:4", "Finch", "Deployment Strategist", "2026-11-01", interest="4"),
            row("greenhouse:finch:5", "Finch", "Deployment Strategist", "2026-11-09"),
            row("ashby:other:6", "Finch", "Business Operations Associate", "2026-11-09")]
    kept = {r["posting_id"] for r in _dedupe(rows)}
    assert kept == {"jobspipe:1", "ashby:clay:9", "jsearch:4", "ashby:other:6"}


def test_contact_sheets_shared_with_the_pipeline_are_added_once(criteria, board, evals):
    from src.output.sheet import CONTACT_HEADERS
    store, sheet = Store(":memory:"), FakeBackend()
    grid = [["Company", "Name", "Title", "Covers", "LinkedIn", "Connection", "Wave", "Why", "Angle", "Write-up",
             "Note sent", "Follow up", "Status"],
            ["EliseAI", "Marcus Frankel", "Strategy Manager", "Growth Strategy Analyst", "https://x", "2nd", "1",
             "Why text", "Angle text", "Hi Marcus", "", "", "Not sent"],
            ["EliseAI", "Alison Nordell", "Strategy", "Growth Strategy Analyst", "https://y", "3rd", "Hold",
             "", "", "", "", "", "On file"]]
    sheet.contact_imports = lambda: [("Contacts - EliseAI", grid)]
    run(store, sheet, board, evals, criteria, T1)
    rows = sheet.read(["Contacts"])["Contacts"]
    assert [r[1] for r in rows[1:3]] == ["Marcus Frankel", "Alison Nordell"]
    assert rows[1][CONTACT_HEADERS.index("Why")] == "Why text" and rows[1][CONTACT_HEADERS.index("Status")] == "Not sent"
    sheet.set_cell("Contacts", 1, CONTACT_HEADERS.index("Status"), "Sent")       # your edit
    run(store, sheet, board, evals, criteria, T2)
    rows = sheet.read(["Contacts"])["Contacts"]
    assert len([r for r in rows[1:] if any(r)]) == 2                               # not added twice
    assert rows[1][CONTACT_HEADERS.index("Status")] == "Sent"                    # your edit kept

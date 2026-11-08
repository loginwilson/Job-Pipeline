"""Triage Sheet: Decision / Interest / Status, three tabs, applied date, follow-up, one write per run."""
import copy

import pytest
from conftest import FIXTURES

from src.config import Company
from src.output.sheet import ALL_TABS, HEADERS as H, ID_COL, JOBS, FakeBackend, sync_all

# One table since Nov 8: every job is one row on Jobs; "My picks" and "Passed" are live
# formula views of it. Tests compute those views from the Jobs rows, as the formulas do.
REVIEW = JOBS
APPLICATIONS, PASSED = "__view: my picks", "__view: passed"
from src.run import evaluate_company, store_evaluations
from src.sources import load_fixture
from src.store.db import Store

T1, T2 = "2026-11-05T10:17:00+00:00", "2026-11-05T11:17:00+00:00"
T_LATER = "2026-11-16T11:17:00+00:00"        # 11 days after T1
# one pick column since Nov 8: Interest holds Pass / 1-5 ("Apply" typed there still counts)
DEC, INT, STATUS, APPLIED, FLAGS, ROLE, NOTES = (H.index(x) for x in
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
    if name in (APPLICATIONS, PASSED):
        jobs = tab(sheet, JOBS)
        if name == PASSED:
            return [r for r in jobs if r[INT] == "Pass"]
        picks = [r for r in jobs if r[INT].isdigit() and int(r[INT]) >= 1]
        return sorted(picks, key=lambda r: -int(r[INT]))         # SORT(..., 1, FALSE), stable
    rows = sheet.read([name])[name]
    return [r + [""] * (len(H) - len(r)) for r in rows[1:]]


def row_index(sheet, name, pid):
    return 1 + [r[ID_COL] for r in tab(sheet, name)].index(pid)


def test_new_postings_land_in_review(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    info = run(store, sheet, board, evals, criteria, T1)
    passed = sum(e.passed for e in evals)
    assert info["tabs"] == {"Jobs": passed, "My picks": 0, "Passed": 0}
    assert sheet.read([REVIEW])[REVIEW][0] == H
    assert all(r[DEC] == "" and "New" in r[FLAGS] for r in tab(sheet, REVIEW))
    assert set(sheet.tabs) == set(ALL_TABS) and len(sheet.calls) == 1       # one write per run
    assert sheet.tabs["Home"]["index"] == 0 and sheet.tabs["Jobs"]["index"] == 1        # home, then your jobs
    assert sheet.tabs["My picks"]["grid"][0][0]["formulaValue"].startswith("={Jobs!A1:")   # a live view


def test_decisions_move_rows_and_survive(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    ids = [r[ID_COL] for r in tab(sheet, REVIEW)]
    a, b, c, d = ids[:4]
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, a), DEC, "Apply")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, a), INT, "5")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, a), NOTES, "Coffee chat")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, b), DEC, "Pass")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, c), INT, "3")            # Interest alone implies Apply
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, c), STATUS, "Applied")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, d), DEC, "Apply")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, d), INT, "4")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, d), STATUS, "Rejected")
    run(store, sheet, board, evals, criteria, T2)

    apps = tab(sheet, APPLICATIONS)
    assert [r[ID_COL] for r in apps] == [a, d, c]          # highest Interest first
    assert apps[0][INT] == "5" and apps[0][NOTES] == "Coffee chat"
    assert apps[2][APPLIED] == T2[:10]                       # Applied on stamped automatically
    assert [r[ID_COL] for r in tab(sheet, PASSED)] == [b]
    review = tab(sheet, REVIEW)
    # one row per job: picks and passes stay on Jobs (the views just show them)
    assert {a, b, c, d} <= {r[ID_COL] for r in review} and len(review) == len({r[ID_COL] for r in review})
    assert not any("New" in r[FLAGS].split(", ") for r in review)

    run(store, sheet, board, evals, criteria, T2)            # a third run keeps everything in place
    assert [r[ID_COL] for r in tab(sheet, APPLICATIONS)] == [a, d, c]


def test_follow_up_after_ten_days(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    pid = tab(sheet, REVIEW)[0][ID_COL]
    sheet.set_cell(REVIEW, 1, DEC, "Apply")
    sheet.set_cell(REVIEW, 1, STATUS, "Applied")
    run(store, sheet, board, evals, criteria, T2)
    assert "Follow up" not in tab(sheet, APPLICATIONS)[0][FLAGS]
    run(store, sheet, board, evals, criteria, T_LATER)
    app = next(r for r in tab(sheet, APPLICATIONS) if r[ID_COL] == pid)
    assert "Follow up" in app[FLAGS] and app[APPLIED] == T2[:10]


def test_owner_values_restored_from_database(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    sheet.set_cell(REVIEW, 1, DEC, "Apply")
    sheet.set_cell(REVIEW, 1, INT, "4")
    pid = tab(sheet, REVIEW)[0][ID_COL]
    run(store, sheet, board, evals, criteria, T2)
    fresh = FakeBackend()                                    # tabs deleted / new Sheet
    run(store, fresh, board, evals, criteria, T2)
    app = next(r for r in tab(fresh, APPLICATIONS) if r[ID_COL] == pid)
    assert app[INT] == "4"


def test_hyperlinks_and_formatting(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    assert all(f.startswith('=HYPERLINK("https://jobs.lever.co/') for f in sheet.formulas(REVIEW, ROLE))
    t = sheet.tabs[JOBS]
    assert t["frozen"] == 1 and len(t["cf"]) == 7
    kinds = [next(iter(r)) for r in t["other"]]
    assert kinds.count("setDataValidation") == 3 and "setBasicFilter" in kinds
    assert len(sheet.tabs["My picks"]["cf"]) == 4 and sheet.tabs["Passed"]["frozen"] == 1
    run(store, sheet, board, evals, criteria, T2)
    assert len(sheet.tabs[REVIEW]["cf"]) == 7                # rules not duplicated


def test_closed_postings_leave_review_but_stay_in_applications(criteria, board, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evaluate_company(board, payload, criteria), criteria, T1)
    rows = tab(sheet, REVIEW)
    untouched, applied = rows[0][ID_COL], rows[1][ID_COL]
    sheet.set_cell(REVIEW, 2, DEC, "Apply")
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
    sheet.set_cell(REVIEW, 1, NOTES, "keep me")
    pid = tab(sheet, REVIEW)[0][ID_COL]
    store.conn.execute("UPDATE scores SET passed=0 WHERE posting_id=?", (pid,))   # e.g. criteria tightened
    store.commit()
    sync_all(sheet, store, criteria, T2)
    kept = [r for name in (REVIEW, APPLICATIONS, PASSED) for r in tab(sheet, name) if r[ID_COL] == pid]
    assert kept and kept[0][NOTES] == "keep me"


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
    assert sorted(flags, key=_preference_rank) == [flags[3], flags[2], flags[1], flags[0]]


def test_companies_tab_target_and_not_for_me(criteria, board, evals):
    """Pipeline 2: your Companies-tab Status steers To review."""
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    company = tab(sheet, REVIEW)[0][H.index("Company")]
    store.note_target_company(company, "AI × real estate", "About", "test", T1)
    store.commit()
    sync_all(sheet, store, criteria, T1)
    rows = sheet.read(["Companies"])["Companies"]
    assert rows[0][:3] == ["Interest", "Status", "Company"] and any(r[2] == company for r in rows[1:])
    i = 1 + [r[2] for r in rows[1:]].index(company)
    sheet.set_cell("Companies", i, 1, "Target")
    sync_all(sheet, store, criteria, T2)
    assert all("Target company" in r[H.index("Flags")] for r in tab(sheet, REVIEW) if r[H.index("Company")] == company)
    rows = sheet.read(["Companies"])["Companies"]
    i = 1 + [r[2] for r in rows[1:]].index(company)
    sheet.set_cell("Companies", i, 1, "Not for me")
    sync_all(sheet, store, criteria, T2)
    assert not any(r[H.index("Company")] == company for r in tab(sheet, REVIEW))


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


def test_company_jobs_add_moves_a_dropped_job_to_my_applications(criteria):
    store, sheet = Store(":memory:"), FakeBackend()
    store.upsert_posting("ashby:decagon:1", "Decagon", "Revenue Operations Associate", "https://x/1", None, None, T1)
    store.save_score("ashby:decagon:1", False, [{"gate": "experience", "matched": "2+ yrs required: 2-4 years"}],
                     [], {"experience": "2+ yrs required"}, T1)
    store.note_target_company("Decagon", "AI-forward", "AI support agents", "test", T1)
    store.save_company_owner("Decagon", {"Status": "Target"}, T1)
    store.commit()
    sync_all(sheet, store, criteria, T1)
    jobs = sheet.read(["Company jobs"])["Company jobs"]
    assert jobs[0][0] == "Add" and len(jobs) == 2 and jobs[1][3].startswith("No: experience")
    sheet.set_cell("Company jobs", 1, 0, "Add")
    sync_all(sheet, store, criteria, T2)
    apps = tab(sheet, APPLICATIONS)
    assert [r[ID_COL] for r in apps] == ["ashby:decagon:1"] and apps[0][INT] == "3"     # added = a 3
    assert len([r for r in sheet.read(["Company jobs"])["Company jobs"][1:] if any(r)]) == 0


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
    flags = tab(sheet, APPLICATIONS)[0][H.index("Flags")]
    assert "Outside your rules now (field)" in flags and "Closed" not in flags
    assert store.is_tracked("ashby:meow:1")



def test_one_row_per_job_your_edits_and_order_stay(criteria, board, evals):
    """One table: a job is one row; your edits stick and your sort order is kept between runs."""
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    ids = [r[ID_COL] for r in tab(sheet, REVIEW)]
    pid = ids[-1]
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, pid), INT, "5")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, pid), STATUS, "Applied")
    # you sort the whole table (every column, ID included): reverse it
    grid = sheet.tabs[REVIEW]["grid"]
    grid[1:len(ids) + 1] = list(reversed(grid[1:len(ids) + 1]))
    run(store, sheet, board, evals, criteria, T2)
    after = tab(sheet, REVIEW)
    assert [r[ID_COL] for r in after] == list(reversed(ids))      # your order kept
    row = after[0]
    assert row[ID_COL] == pid and row[INT] == "5" and row[STATUS] == "Applied"
    assert [r[ID_COL] for r in tab(sheet, APPLICATIONS)] == [pid]


def test_grad_programs_tab_lists_programs_with_lighter_rules(criteria):
    store, sheet = Store(":memory:"), FakeBackend()
    def put(pid, title, passed, failures):
        store.upsert_posting(pid, "Co", title, f"https://x/{pid}", None, None, T1)
        store.save_score(pid, passed, failures, [], {}, T1)
    put("a:1", "The Wolf Graduate Program (2027)", False, [{"gate": "salary", "matched": "midpoint USD 90,000 below floor"}])
    put("a:2", "APX (New Grad 2027)", False, [{"gate": "field", "matched": "not one of your fields: APX"}])
    put("a:3", "Software Engineer, New Grad", False, [{"gate": "technical", "matched": "engineer"}])
    put("a:4", "Deployment Strategist, New Grad - Commercial", False,
        [{"gate": "location", "matched": "no listed city in: Seoul, South Korea"}])
    put("a:5", "Operations Associate (University Hire)", True, [])
    put("a:6", "Business Operations Associate", True, [])
    store.commit()
    sync_all(sheet, store, criteria, T1)
    rows = [r for r in sheet.read(["Grad programs"])["Grad programs"][1:] if any(r)]
    ids = [r[7] for r in rows]
    assert ids == ["a:5", "a:2", "a:1"] or set(ids) == {"a:5", "a:2", "a:1"}
    assert rows[0][3].startswith("✅")
    # Add from the Grad programs tab lands in My applications
    i = 1 + ids.index("a:1")
    sheet.set_cell("Grad programs", i, 0, "Add")
    sync_all(sheet, store, criteria, T2)
    assert "a:1" in [r[ID_COL] for r in tab(sheet, APPLICATIONS)]
    assert any("Grad program" in r[FLAGS] for r in tab(sheet, REVIEW) if r[ID_COL] == "a:5")


def test_a_company_you_type_on_the_companies_tab_is_kept(criteria):
    store, sheet = Store(":memory:"), FakeBackend()
    sync_all(sheet, store, criteria, T1)
    rows = sheet.read(["Companies"])["Companies"]
    n = len([r for r in rows if any(r)])
    sheet.set_cell("Companies", n, 2, "Palantir Technologies")
    sheet.set_cell("Companies", n, 1, "Target")
    sync_all(sheet, store, criteria, T2)
    rows = sheet.read(["Companies"])["Companies"]
    hit = [r for r in rows if len(r) > 2 and r[2] == "Palantir Technologies"]
    from src.output.sheet import COMPANY_HEADERS
    assert hit and hit[0][1] == "Target" and hit[0][COMPANY_HEADERS.index("Source")] == "you added on the Sheet"


def test_home_contacts_and_company_hub(criteria, board, evals):
    """Contacts are yours (never rewritten); they roll up to Companies and Home's to-do list."""
    from src.output.sheet import COMPANY_HEADERS, CONTACT_HEADERS
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    assert sheet.read(["Contacts"])["Contacts"][0] == CONTACT_HEADERS
    crow = 1 + [r[2] for r in sheet.read(["Companies"])["Companies"][1:]].index("Fora")   # on your list
    for h, v in (("Interest", "5"), ("Status", "Networking"), ("Next step", "Coffee chat ask"), ("Due", "2026-11-09")):
        sheet.set_cell("Companies", crow, COMPANY_HEADERS.index(h), v)
    for col, v in enumerate(["Fora", "Sasha", "Product Ops", "NYU alum", "", "Messaged", "2026-10-25",
                             "Ask about Wolf program", "2026-11-08", ""]):
        sheet.set_cell("Contacts", 1, col, v)
    sync_all(sheet, store, criteria, T2)
    contacts = sheet.read(["Contacts"])["Contacts"]
    assert contacts[1][1] == "Sasha"                          # your row untouched
    comp = {r[2]: r for r in sheet.read(["Companies"])["Companies"][1:] if len(r) > 2}
    fora = comp["Fora"]
    assert fora[COMPANY_HEADERS.index("Connections")].startswith("1: Sasha (Messaged)")
    assert fora[COMPANY_HEADERS.index("Last contact")] == "2026-10-25"
    home = [" | ".join(r) for r in sheet.read(["Home"])["Home"]]
    text = "\n".join(home)
    assert "Ask about Wolf program" in text and "Coffee chat ask" in text and "No reply since 2026-10-25" in text
    assert "STANDOUT COMPANIES" in text and "Fora | Networking | 5" in text



def test_one_pick_column_pass_and_numbers(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    run(store, sheet, board, evals, criteria, T1)
    a, b, c = [r[ID_COL] for r in tab(sheet, REVIEW)][:3]
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, a), INT, "5")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, b), INT, "Pass")
    sheet.set_cell(REVIEW, row_index(sheet, REVIEW, c), INT, "Apply")
    run(store, sheet, board, evals, criteria, T2)
    review = {r[ID_COL]: r for r in tab(sheet, REVIEW)}
    assert review[a][INT] == "5" and review[b][INT] == "Pass" and review[c][INT] == "3"
    assert {a, c} <= {r[ID_COL] for r in tab(sheet, APPLICATIONS)}
    assert [r[ID_COL] for r in tab(sheet, PASSED)] == [b]
    assert sheet.read([REVIEW])[REVIEW][0][:4] == ["Interest", "Status", "Next step", "Due"]


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


def test_reset_backs_up_old_picks_wipes_scores_and_removes_old_tabs(criteria, board, evals):
    store, sheet = Store(":memory:"), FakeBackend()
    store_evaluations(store, board, evals, T1, criteria)
    pid = store.conn.execute("SELECT posting_id FROM postings LIMIT 1").fetchone()[0]
    store.save_owner_state(pid, {"Decision": "Apply", "Interest": "4", "Status": "", "Notes": "liked"}, T1)
    store.commit()
    sheet.ensure_tabs(["To review", "My applications", "Passed"])          # the old layout
    sync_all(sheet, store, criteria, T1)
    assert "To review" not in sheet.tabs and "My applications" not in sheet.tabs
    assert store.conn.execute("SELECT COUNT(*) FROM owner_state").fetchone()[0] == 0
    backup = sheet.read(["Before reset"])["Before reset"]
    assert backup[0][:3] == ["Your pick", "Status", "Company"] and backup[1][0] == "4" and backup[1][4] == "liked"
    assert all(r[INT] == "" for r in tab(sheet, JOBS))                     # fresh start
    sync_all(sheet, store, criteria, T2)                                   # only once
    assert len(sheet.read(["Before reset"])["Before reset"]) == 2

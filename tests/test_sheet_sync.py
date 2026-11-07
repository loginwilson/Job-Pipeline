"""Store + Sheet sync: owner columns survive runs, hyperlinks, closed rows, one write per run."""
from src.output.sheet import HEADERS as H
import copy

import pytest
from conftest import FIXTURES

from src.config import load_watchlist
from src.output.sheet import HEADERS, ID_COL, PIPELINE, FakeBackend, sync_pipeline
from src.run import evaluate_company, store_evaluations
from src.sources import load_fixture
from src.store.db import Store

T1, T2, T3 = "2026-11-05T10:17:00+00:00", "2026-11-05T11:17:00+00:00", "2026-11-05T12:17:00+00:00"


@pytest.fixture(scope="module")
def palantir():
    return next(c for c in load_watchlist() if c.name == "Palantir")


@pytest.fixture(scope="module")
def payload():
    return load_fixture(FIXTURES / "lever_palantir.json.gz")


def run_once(store, backend, company, payload, criteria, ts):
    evals = evaluate_company(company, payload, criteria)
    store_evaluations(store, company, evals, ts)
    store.commit()
    return sync_pipeline(backend, store, criteria, ts)


DEC, STATUS, NOTES, FLAGS, ROLE = (H.index(x) for x in ("Decision", "Status", "Notes", "Flags", "Role"))


def rows_by_id(backend):
    rows = backend.read([PIPELINE])[PIPELINE]
    return rows[0], {r[ID_COL]: r + [""] * (len(HEADERS) - len(r)) for r in rows[1:]}


def test_owner_columns_survive_second_run(criteria, palantir, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run_once(store, sheet, palantir, payload, criteria, T1)
    header, rows = rows_by_id(sheet)
    assert header == HEADERS and len(rows) == 8
    assert all("New" in r[FLAGS] for r in rows.values())

    # owner fills Status / Effort / Notes on one row
    grid_row = next(i for i, r in enumerate(sheet.read([PIPELINE])[PIPELINE]) if "Deployment Strategist" in r[ROLE:ROLE + 1])
    sheet.set_cell(PIPELINE, grid_row, DEC, "All-in")
    sheet.set_cell(PIPELINE, grid_row, STATUS, "Networking")
    sheet.set_cell(PIPELINE, grid_row, NOTES, "Coffee chat with DS team")
    pid = sheet.read([PIPELINE])[PIPELINE][grid_row][ID_COL]

    calls_before = len(sheet.calls)
    run_once(store, sheet, palantir, payload, criteria, T2)
    assert len(sheet.calls) == calls_before + 1          # one batch_update per run
    _, rows = rows_by_id(sheet)
    assert [rows[pid][i] for i in (DEC, STATUS, NOTES)] == ["All-in", "Networking", "Coffee chat with DS team"]
    assert not any("New" in r[FLAGS].split(", ") for r in rows.values())   # no longer new
    assert len(rows) == 8                                   # no duplicate rows


def test_hyperlinks_and_formatting(criteria, palantir, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run_once(store, sheet, palantir, payload, criteria, T1)
    links = sheet.formulas(PIPELINE, ROLE)
    assert links and all(f.startswith('=HYPERLINK("https://jobs.lever.co/palantir/') for f in links)
    tab = sheet.tabs[PIPELINE]
    assert tab["frozen"] == 1 and len(tab["cf"]) == 2
    kinds = {next(iter(r)) for r in tab["other"]}
    assert {"setDataValidation", "setBasicFilter", "repeatCell", "updateDimensionProperties"} <= kinds
    run_once(store, sheet, palantir, payload, criteria, T2)
    assert len(sheet.tabs[PIPELINE]["cf"]) == 2               # rules not duplicated


def test_closed_posting_stays_and_sinks(criteria, palantir, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run_once(store, sheet, palantir, payload, criteria, T1)
    first_id = sheet.read([PIPELINE])[PIPELINE][1][ID_COL]
    native = first_id.split(":")[-1]
    shrunk = [p for p in copy.deepcopy(payload) if p["id"] != native]
    run_once(store, sheet, palantir, shrunk, criteria, T2)
    rows = sheet.read([PIPELINE])[PIPELINE]
    assert rows[-1][ID_COL] == first_id and "Closed" in rows[-1][FLAGS]
    run_once(store, sheet, palantir, payload, criteria, T3)    # reappears -> reopened
    rows = sheet.read([PIPELINE])[PIPELINE]
    assert "Closed" not in next(r for r in rows if r[ID_COL] == first_id)[FLAGS]


def test_touched_row_never_deleted(criteria, palantir, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run_once(store, sheet, palantir, payload, criteria, T1)
    sheet.set_cell(PIPELINE, 1, NOTES, "keep me")
    pid = sheet.read([PIPELINE])[PIPELINE][1][ID_COL]
    store.conn.execute("UPDATE scores SET passed=0 WHERE posting_id=?", (pid,))   # e.g. criteria tightened
    store.commit()
    sync_pipeline(sheet, store, criteria, T2)
    _, rows = rows_by_id(sheet)
    assert pid in rows and rows[pid][NOTES] == "keep me"


def test_first_seen_persists(criteria, palantir, payload):
    store = Store(":memory:")
    for ts in (T1, T2):
        store_evaluations(store, palantir, evaluate_company(palantir, payload, criteria), ts)
    row = store.conn.execute("SELECT first_seen, last_seen, is_new FROM postings LIMIT 1").fetchone()
    assert (row["first_seen"], row["last_seen"], row["is_new"]) == (T1, T2, 0)


def test_sync_all_tabs_one_write(criteria, palantir, payload):
    from src.output.sheet import ALL_TABS, sync_all
    store, sheet = Store(":memory:"), FakeBackend()
    evals = evaluate_company(palantir, payload, criteria)
    store_evaluations(store, palantir, evals, T1, criteria)
    store.board_ok("Palantir", "lever", len(evals), T1)
    store.record_run(started_at=T1, finished_at=T1, boards_polled=1, postings_seen=309, new=309,
                     passed=8, errors=0)
    store.commit()
    sync_all(sheet, store, criteria, T1)
    assert len(sheet.calls) == 1
    data = sheet.read(ALL_TABS)
    assert data["Dropped"][0] == ["Company", "Role", "Gate", "Matched text", "Date"] and len(data["Dropped"]) == 302
    assert data["Funnel"][1][1:] == ["1", "309", "309", "8", "301", "0"]
    assert data["Board health"][1][-1] == "OK"
    pipe = data["Pipeline"]
    assert pipe[0] == H and "Tier" not in pipe[0] and "Score" not in pipe[0]
    assert all(r[H.index("Field")] for r in pipe[1:]) and all(r[H.index("Source")] == "Lever" for r in pipe[1:])


def test_decision_sorting_and_veto(criteria, palantir, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run_once(store, sheet, palantir, payload, criteria, T1)
    rows = sheet.read([PIPELINE])[PIPELINE]
    vetoed, applied, allin = rows[1][ID_COL], rows[2][ID_COL], rows[3][ID_COL]
    sheet.set_cell(PIPELINE, 1, DEC, "Veto")
    sheet.set_cell(PIPELINE, 2, DEC, "Apply")
    sheet.set_cell(PIPELINE, 3, DEC, "All-in")
    run_once(store, sheet, palantir, payload, criteria, T2)
    order = [r[ID_COL] for r in sheet.read([PIPELINE])[PIPELINE][1:]]
    decisions = [r[DEC] if len(r) > DEC else "" for r in sheet.read([PIPELINE])[PIPELINE][1:]]
    assert order[-1] == vetoed and order[-3] == allin and order[-2] == applied
    assert decisions == [""] * 5 + ["All-in", "Apply", "Veto"]   # undecided first, then All-in, Apply, Veto

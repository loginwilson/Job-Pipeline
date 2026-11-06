"""Store + Sheet sync: owner columns survive runs, hyperlinks, closed rows, one write per run."""
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


def rows_by_id(backend):
    rows = backend.read([PIPELINE])[PIPELINE]
    return rows[0], {r[ID_COL]: r + [""] * (len(HEADERS) - len(r)) for r in rows[1:]}


def test_owner_columns_survive_second_run(criteria, palantir, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run_once(store, sheet, palantir, payload, criteria, T1)
    header, rows = rows_by_id(sheet)
    assert header == HEADERS and len(rows) == 6
    assert all("New" in r[10] for r in rows.values())

    # owner fills Status / Effort / Notes on one row
    grid_row = next(i for i, r in enumerate(sheet.read([PIPELINE])[PIPELINE]) if "Deployment Strategist" in r[3:4])
    sheet.set_cell(PIPELINE, grid_row, 14, "Shortlisted")
    sheet.set_cell(PIPELINE, grid_row, 15, "Deep")
    sheet.set_cell(PIPELINE, grid_row, 16, "Ask about Warp Speed")
    pid = sheet.read([PIPELINE])[PIPELINE][grid_row][ID_COL]

    calls_before = len(sheet.calls)
    run_once(store, sheet, palantir, payload, criteria, T2)
    assert len(sheet.calls) == calls_before + 1          # one batch_update per run
    _, rows = rows_by_id(sheet)
    assert rows[pid][14:17] == ["Shortlisted", "Deep", "Ask about Warp Speed"]
    assert not any("New" in r[10].split(", ") for r in rows.values())   # no longer new
    assert len(rows) == 6                                   # no duplicate rows


def test_hyperlinks_and_formatting(criteria, palantir, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run_once(store, sheet, palantir, payload, criteria, T1)
    links = sheet.formulas(PIPELINE, 3)
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
    assert rows[-1][ID_COL] == first_id and "Closed" in rows[-1][10]
    run_once(store, sheet, palantir, payload, criteria, T3)    # reappears -> reopened
    rows = sheet.read([PIPELINE])[PIPELINE]
    assert "Closed" not in next(r for r in rows if r[ID_COL] == first_id)[10]


def test_touched_row_never_deleted(criteria, palantir, payload):
    store, sheet = Store(":memory:"), FakeBackend()
    run_once(store, sheet, palantir, payload, criteria, T1)
    sheet.set_cell(PIPELINE, 1, 16, "keep me")
    pid = sheet.read([PIPELINE])[PIPELINE][1][ID_COL]
    store.conn.execute("UPDATE scores SET passed=0 WHERE posting_id=?", (pid,))   # e.g. criteria tightened
    store.commit()
    sync_pipeline(sheet, store, criteria, T2)
    _, rows = rows_by_id(sheet)
    assert pid in rows and rows[pid][16] == "keep me"


def test_first_seen_persists(criteria, palantir, payload):
    store = Store(":memory:")
    for ts in (T1, T2):
        store_evaluations(store, palantir, evaluate_company(palantir, payload, criteria), ts)
    row = store.conn.execute("SELECT first_seen, last_seen, is_new FROM postings LIMIT 1").fetchone()
    assert (row["first_seen"], row["last_seen"], row["is_new"]) == (T1, T2, 0)

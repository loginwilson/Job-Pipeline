"""Google Sheet sync — the Sheet is a view of SQLite.

Per run: ONE read (values of the tabs) + ONE spreadsheets.batchUpdate (all
writes and formatting). Tabs are only created on the first run.

Pipeline tab rules (BUILD_BRIEF Step 6):
  * upsert by Posting ID (hidden col R); A–N are rewritten from the DB,
    O–Q (Status / Effort / Notes) are owner-edited and always carried over;
  * a row the owner touched is never deleted, even if it left the DB view;
  * closed postings stay (Flags has "Closed") and sort to the bottom;
  * sort: open first, Tier (Strong first), Score desc, First seen desc.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from ..config import Criteria
from ..store.db import Store

PIPELINE = "Pipeline"
HEADERS = ["Tier", "Score", "Company", "Role", "Role type", "Requirements", "Location", "Salary",
           "Extra comp", "Experience ask", "Flags", "Why", "First seen", "Posted",
           "Status", "Effort", "Notes", "Posting ID"]
OWNER_COLS = (14, 15, 16)          # O, P, Q
ID_COL = 17                        # R
STATUS_OPTIONS = ["Shortlisted", "Drafting", "Applied", "Interviewing", "Offer", "Rejected", "Pass"]
EFFORT_OPTIONS = ["Deep", "Standard", "Quick"]
TIER_ORDER = {"Strong": 0, "Worth a look": 1, "Stored": 2}
GREEN = {"red": 0.85, "green": 0.94, "blue": 0.83}
GRAY = {"red": 0.6, "green": 0.6, "blue": 0.6}


class SheetBackend(Protocol):
    def read(self, tabs: list[str]) -> dict[str, list[list[str]]]: ...
    def ensure_tabs(self, tabs: list[str]) -> dict[str, dict]: ...   # name -> {id, rows, cols, has_cf}
    def batch_update(self, requests: list[dict]) -> None: ...


# --- helpers ------------------------------------------------------------
def hyperlink(url: str, text: str) -> str:
    def q(s: str) -> str:
        return (s or "").replace('"', '""')
    return f'=HYPERLINK("{q(url)}","{q(text)}")'


def _cell(v: Any) -> dict:
    if v is None or v == "":
        return {}
    if isinstance(v, bool):
        return {"userEnteredValue": {"stringValue": str(v)}}
    if isinstance(v, (int, float)):
        return {"userEnteredValue": {"numberValue": v}}
    s = str(v)
    if s.startswith("="):
        return {"userEnteredValue": {"formulaValue": s}}
    return {"userEnteredValue": {"stringValue": s}}


def _date(iso: str | None) -> str:
    return (iso or "")[:10]


def _pad(row: list, n: int) -> list:
    return list(row) + [""] * (n - len(row))


# --- row building ---------------------------------------------------------
def build_pipeline_rows(store: Store, criteria: Criteria, ts: str) -> list[dict]:
    fresh = criteria.get("freshness", default={}) or {}
    apply_days = int(fresh.get("apply_soon_days", 7))
    now = datetime.fromisoformat(ts)
    rows = []
    for r in store.pipeline_rows():
        d = json.loads(r["display"] or "{}")
        flags = []
        if r["is_new"] and fresh.get("flag_new_since_last_check", True):
            flags.append("New")
        posted = r["posted_at"] or r["first_seen"]
        if not r["closed_at"] and posted:
            try:
                age = now - datetime.fromisoformat(posted.replace("Z", "+00:00"))
                if age <= timedelta(days=apply_days):
                    flags.append("Apply soon")
            except ValueError:
                pass
        flags += json.loads(r["flags"] or "[]")
        if r["evergreen"]:
            flags.append("Evergreen")
        if r["closed_at"]:
            flags.append("Closed")
        rows.append({
            "posting_id": r["posting_id"], "closed": bool(r["closed_at"]),
            "tier": r["tier"] or "", "score": r["final_score"] if r["final_score"] is not None else
            (r["requirements_score"] if r["requirements_score"] is not None else ""),
            "first_seen": r["first_seen"],
            "sort": r["sort_score"] if r["sort_score"] is not None else None,
            "values": [
                r["tier"] or "",
                None,  # score filled below
                ("★ " if d.get("anchor") else "") + r["company"],
                hyperlink(r["url"], r["title"]),
                r["role_type"] or "",
                " · ".join(x for x in (r["requirements_summary"], r["tn_label"]) if x),
                d.get("location", ""), d.get("salary", ""), d.get("extra_comp", ""),
                d.get("experience", ""), ", ".join(dict.fromkeys(flags)), r["why"] or "",
                _date(r["first_seen"]), _date(r["posted_at"]),
            ],
        })
        rows[-1]["values"][1] = rows[-1]["score"]
    def _key(x):
        s = x["sort"] if x["sort"] is not None else x["score"]
        return (x["closed"], TIER_ORDER.get(x["tier"], 3),
                -(s if isinstance(s, (int, float)) else -1), _neg_date(x["first_seen"]))
    rows.sort(key=_key)   # tier by fit; within a tier, preferences decide the order
    return rows


def _neg_date(iso: str | None) -> float:
    try:
        return -datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --- sync -----------------------------------------------------------------
def sync_pipeline(backend: SheetBackend, store: Store, criteria: Criteria, ts: str | None = None) -> dict:
    """Pipeline tab only (one read + one write)."""
    ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = backend.ensure_tabs([PIPELINE])
    existing = backend.read([PIPELINE]).get(PIPELINE, [])
    requests, info = pipeline_requests(meta, existing, store, criteria, ts)
    backend.batch_update(requests)
    return info


def pipeline_requests(meta: dict, existing: list[list[str]], store: Store, criteria: Criteria,
                      ts: str) -> tuple[list[dict], dict]:
    owner: dict[str, list[str]] = {}
    orphan_rows: list[list[str]] = []
    db_rows = build_pipeline_rows(store, criteria, ts)
    db_ids = {r["posting_id"] for r in db_rows}
    for row in existing[1:]:
        row = _pad(row, len(HEADERS))
        pid = row[ID_COL]
        if not pid:
            continue
        owner[pid] = [row[i] for i in OWNER_COLS]
        if pid not in db_ids and any(row[i] for i in OWNER_COLS):
            orphan_rows.append(row)     # owner touched it: never delete

    grid = [HEADERS]
    for r in db_rows:
        vals = r["values"] + owner.get(r["posting_id"], ["", "", ""]) + [r["posting_id"]]
        grid.append(vals)
    grid += orphan_rows
    total_rows = max(len(grid), len(existing))
    blank = [""] * len(HEADERS)
    padded = grid + [blank] * (total_rows - len(grid))

    sid = meta[PIPELINE]["id"]
    need_rows = max(meta[PIPELINE].get("rows", 1000), total_rows + 1)
    requests: list[dict] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {
                "rowCount": need_rows, "columnCount": max(meta[PIPELINE].get("cols", 26), len(HEADERS)),
                "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {
            "start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
            "rows": [{"values": [_cell(v) for v in row]} for row in padded],
            "fields": "userEnteredValue"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold"}},
        _validation(sid, 14, STATUS_OPTIONS, need_rows),
        _validation(sid, 15, EFFORT_OPTIONS, need_rows),
        {"setBasicFilter": {"filter": {"range": {
            "sheetId": sid, "startRowIndex": 0, "endRowIndex": len(grid),
            "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": ID_COL, "endIndex": ID_COL + 1},
            "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
    ]
    if not meta[PIPELINE].get("has_cf"):
        full = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}
        requests += [
            {"addConditionalFormatRule": {"index": 0, "rule": {"ranges": [full], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=REGEXMATCH($K2,"Closed")'}]},
                "format": {"textFormat": {"foregroundColor": GRAY}}}}}},
            {"addConditionalFormatRule": {"index": 1, "rule": {"ranges": [full], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=$A2="Strong"'}]},
                "format": {"backgroundColor": GREEN}}}}},
        ]
    return requests, {"rows": len(grid) - 1, "kept_owner_rows": len(orphan_rows), "requests": len(requests)}


# --- the other tabs (rewritten in full each run) ---------------------------
DROPPED, UNPARSED, FUNNEL, HEALTH = "Dropped", "Unparsed alerts", "Funnel", "Board health"
ALL_TABS = [PIPELINE, DROPPED, UNPARSED, FUNNEL, HEALTH]


def _simple_tab(meta: dict, tab: str, header: list[str], rows: list[list], existing_len: int) -> list[dict]:
    sid = meta[tab]["id"]
    grid = [header] + rows
    total = max(len(grid), existing_len)
    padded = grid + [[""] * len(header)] * (total - len(grid))
    return [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": max(meta[tab].get("rows", 1000), total + 1),
            "columnCount": max(meta[tab].get("cols", 26), len(header)), "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(v) for v in _pad(r, len(header))]} for r in padded],
                         "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
    ]


def other_tab_rows(store: Store, criteria: Criteria, ts: str, unparsed: list[list] | None = None) -> dict:
    days = 7
    dropped = [[r["company"], hyperlink(r["url"], r["title"]), r["gate"], r["matched"], _date(r["dropped_at"])]
               for r in store.dropped_rows(days, ts)]
    funnel = [[r["started_at"], r["boards_polled"], r["postings_seen"], r["new"], r["passed"],
               r["strong"], r["worth_a_look"], r["errors"]] for r in reversed(store.runs())]
    health = []
    for r in store.board_health():
        counts = "" if r["count_last"] is None else (
            f"{r['count_last']} (prev {r['count_prev']})" if r["count_prev"] is not None else str(r["count_last"]))
        health.append([r["company"], r["platform"], r["last_success"] or "",
                       (f"{_date(r['last_error_at'])}: {r['last_error']}" if r["last_error"] else ""),
                       counts, r["status"]])
    return {
        DROPPED: (["Company", "Role", "Gate", "Matched text", "Date"], dropped),
        UNPARSED: (["Sender", "Subject", "Date"], unparsed or []),
        FUNNEL: (["Timestamp", "Boards polled", "Postings seen", "New", "Passed gates", "Strong",
                  "Worth a look", "Errors"], funnel),
        HEALTH: (["Company", "Platform", "Last success", "Last error", "Postings (last vs previous)",
                  "Status"], health),
    }


def sync_all(backend: SheetBackend, store: Store, criteria: Criteria, ts: str | None = None,
             unparsed: list[list] | None = None) -> dict:
    """Every tab: one read + one batchUpdate per run."""
    ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = backend.ensure_tabs(ALL_TABS)
    existing = backend.read(ALL_TABS)
    requests, info = pipeline_requests(meta, existing.get(PIPELINE, []), store, criteria, ts)
    for tab, (header, rows) in other_tab_rows(store, criteria, ts, unparsed).items():
        requests += _simple_tab(meta, tab, header, rows, len(existing.get(tab, [])))
    backend.batch_update(requests)
    info["requests"] = len(requests)
    return info


def _validation(sid: int, col: int, options: list[str], rows: int) -> dict:
    return {"setDataValidation": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": rows,
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST",
                               "values": [{"userEnteredValue": o} for o in options]},
                 "showCustomUi": True, "strict": False}}}


# --- backends ---------------------------------------------------------------
class GspreadBackend:
    """Real Google Sheets via gspread (service account)."""

    def __init__(self, sheet_id: str, service_account_info: dict):
        import gspread
        gc = gspread.service_account_from_dict(service_account_info)
        self.ss = gc.open_by_key(sheet_id)

    def ensure_tabs(self, tabs: list[str]) -> dict[str, dict]:
        meta = self.ss.fetch_sheet_metadata({"fields": "sheets(properties,conditionalFormats)"})
        have = {s["properties"]["title"]: s for s in meta["sheets"]}
        missing = [t for t in tabs if t not in have]
        if missing:
            self.ss.batch_update({"requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]})
            return self.ensure_tabs(tabs)
        out = {}
        for t in tabs:
            p = have[t]["properties"]
            gp = p.get("gridProperties", {})
            out[t] = {"id": p["sheetId"], "rows": gp.get("rowCount", 1000), "cols": gp.get("columnCount", 26),
                      "has_cf": bool(have[t].get("conditionalFormats"))}
        return out

    def read(self, tabs: list[str]) -> dict[str, list[list[str]]]:
        res = self.ss.values_batch_get([f"'{t}'!A:R" for t in tabs])
        return {t: vr.get("values", []) for t, vr in zip(tabs, res.get("valueRanges", []))}

    def batch_update(self, requests: list[dict]) -> None:
        self.ss.batch_update({"requests": requests})


class FakeBackend:
    """In-memory stand-in for tests/demos; optionally persisted to a JSON file.

    Applies addSheet / updateSheetProperties / updateCells and records the
    rest. read() returns what Sheets would display (HYPERLINK -> link text).
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.tabs: dict[str, dict] = {}
        self.calls: list[list[dict]] = []
        if self.path and self.path.exists():
            self.tabs = json.loads(self.path.read_text())

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.tabs, indent=1))

    def ensure_tabs(self, tabs: list[str]) -> dict[str, dict]:
        for t in tabs:
            if t not in self.tabs:
                self.tabs[t] = {"id": len(self.tabs) + 1, "rows": 1000, "cols": 26, "grid": [],
                                "cf": [], "other": []}
        return {t: {"id": self.tabs[t]["id"], "rows": self.tabs[t]["rows"], "cols": self.tabs[t]["cols"],
                    "has_cf": bool(self.tabs[t]["cf"])} for t in tabs}

    def _tab_by_id(self, sid: int) -> dict:
        return next(t for t in self.tabs.values() if t["id"] == sid)

    def batch_update(self, requests: list[dict]) -> None:
        self.calls.append(requests)
        for req in requests:
            (kind, body), = req.items()
            if kind == "updateCells":
                tab = self._tab_by_id(body["start"]["sheetId"])
                r0 = body["start"]["rowIndex"]
                for i, row in enumerate(body["rows"]):
                    while len(tab["grid"]) <= r0 + i:
                        tab["grid"].append([])
                    tab["grid"][r0 + i] = [c.get("userEnteredValue", {}) for c in row["values"]]
                if len(tab["grid"]) > tab["rows"]:
                    raise ValueError("updateCells exceeds grid limits")
            elif kind == "updateSheetProperties":
                tab = self._tab_by_id(body["properties"]["sheetId"])
                gp = body["properties"].get("gridProperties", {})
                tab["rows"] = gp.get("rowCount", tab["rows"])
                tab["cols"] = gp.get("columnCount", tab["cols"])
                tab["frozen"] = gp.get("frozenRowCount", 0)
            elif kind == "addConditionalFormatRule":
                self._tab_by_id(body["rule"]["ranges"][0]["sheetId"])["cf"].append(body)
            else:
                rng = body.get("range") or body.get("filter", {}).get("range") or {}
                if "sheetId" in rng:
                    self._tab_by_id(rng["sheetId"])["other"].append(req)
        self._save()

    @staticmethod
    def _display(v: dict) -> str:
        if not v:
            return ""
        if "formulaValue" in v:
            f = v["formulaValue"]
            if f.startswith("=HYPERLINK("):
                inner = f[len("=HYPERLINK("):-1]
                text = inner.rsplit('","', 1)[-1].rstrip('"')
                return text.replace('""', '"')
            return f
        if "numberValue" in v:
            n = v["numberValue"]
            return str(int(n)) if float(n).is_integer() else str(n)
        return str(v.get("stringValue", ""))

    def read(self, tabs: list[str]) -> dict[str, list[list[str]]]:
        out = {}
        for t in tabs:
            rows = [[self._display(c) for c in row] for row in self.tabs.get(t, {}).get("grid", [])]
            while rows and not any(rows[-1]):
                rows.pop()
            out[t] = [_strip(r) for r in rows]
        return out

    # convenience for tests / owner simulation
    def set_cell(self, tab: str, row: int, col: int, value: str) -> None:
        grid = self.tabs[tab]["grid"]
        grid[row] = _pad(grid[row], col + 1)
        grid[row] = [c if isinstance(c, dict) else {} for c in grid[row]]
        grid[row][col] = {"stringValue": value}
        self._save()

    def formulas(self, tab: str, col: int) -> list[str]:
        return [row[col].get("formulaValue", "") for row in self.tabs[tab]["grid"][1:] if len(row) > col and row[col]]


def _strip(row: list[str]) -> list[str]:
    while row and row[-1] == "":
        row = row[:-1]
    return row


def backend_from_env(fake_path: str | None = None) -> SheetBackend | None:
    if fake_path:
        return FakeBackend(fake_path)
    sheet_id = os.environ.get("SHEET_ID")
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not (sheet_id and creds):
        return None
    return GspreadBackend(sheet_id, json.loads(creds))

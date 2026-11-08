"""Google Sheet sync — the Sheet is a view of SQLite; your columns are yours.

Triage system (no automatic scoring):
  Decision   Pending / Apply / Pass       — do I want it?
  Interest   1–5 (Apply only)              — how much? 4–5 = all-in effort
  Status     Not started → Drafting → Applied → Networking → Interviewing → Offer,
             or Rejected / Withdrawn / No response
  Notes      free text
The pipeline fills "Applied on" the first time it sees Status = Applied and
flags "Follow up" when an application hasn't moved for
criteria.tracking.follow_up_after_days.

Tabs:
  To review        Decision = Pending (new postings land here), newest first
  My applications  Decision = Apply, Interest 5→1, active statuses before ended ones
  Passed           Decision = Pass — archive, change your mind any time
  Dropped / Unparsed alerts / Funnel / Board health
A row moves to its tab on the next run after you change Decision. Owner
values are read from all three tabs by Posting ID (header names, not column
letters), saved in the database, and never overwritten.

Per run: ONE read + ONE spreadsheets.batchUpdate. Tabs are created on first run.
"""
from __future__ import annotations

import hashlib

import json
import re
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from ..config import Criteria
from ..store.db import Store
from ..textmatch import parse_when

log = logging.getLogger(__name__)

REVIEW, APPLICATIONS, PASSED = "To review", "My applications", "Passed"
DECISION_TABS = [REVIEW, APPLICATIONS, PASSED]
HEADERS = ["Decision", "Interest", "Status", "Applied on", "Company", "Role", "Field", "Location",
           "Salary", "Extra comp", "Experience ask", "Visa", "Flags", "Posted", "First seen", "Source",
           "Notes", "Posting ID"]
OWNER_HEADERS = ("Decision", "Interest", "Status", "Notes")
ID_COL = HEADERS.index("Posting ID")
DECISION_OPTIONS = ["Pending", "Apply", "Pass"]
INTEREST_OPTIONS = ["1", "2", "3", "4", "5"]
STATUS_OPTIONS = ["Not started", "Drafting", "Applied", "Networking", "Interviewing", "Offer",
                  "Rejected", "Withdrawn", "No response"]
ENDED = {"Rejected", "Withdrawn", "No response"}
STATUS_ORDER = {"Offer": 0, "Interviewing": 1, "Networking": 2, "Applied": 3, "Drafting": 4,
                "Not started": 5, "": 5, "Rejected": 7, "Withdrawn": 8, "No response": 9}
PASS_WORDS = {"pass", "veto"}
GREEN = {"red": 0.85, "green": 0.94, "blue": 0.83}
YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.75}
GRAY = {"red": 0.6, "green": 0.6, "blue": 0.6}


def _col_letter(i: int) -> str:
    return chr(ord("A") + i)


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


def _neg_date(iso: str | None) -> float:
    try:
        dt = parse_when(iso)
        return -dt.timestamp() if dt else 0.0
    except ValueError:
        return 0.0


def route(owner: dict) -> str:
    """Which tab a posting belongs in, from your Decision (Interest/Status imply Apply)."""
    d = (owner.get("Decision") or "").strip().lower()
    if d in PASS_WORDS:
        return PASSED
    if d in ("apply", "all-in") or owner.get("Interest") or (owner.get("Status") or "") not in ("", "Not started"):
        return APPLICATIONS
    return REVIEW


def _interest(v: str) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# --- reading your columns ---------------------------------------------------
def read_owner_values(existing: dict[str, list[list[str]]]) -> tuple[dict[str, dict], dict[str, dict]]:
    """Owner cells per Posting ID from the decision tabs (+ full rows, for rows to keep)."""
    owner: dict[str, dict] = {}
    full: dict[str, dict] = {}
    for tab in DECISION_TABS:
        rows = existing.get(tab) or []
        if not rows:
            continue
        idx = {h: i for i, h in enumerate(rows[0])}
        if "Posting ID" not in idx:
            continue
        for raw in rows[1:]:
            raw = _pad(raw, len(rows[0]))
            pid = raw[idx["Posting ID"]]
            if not pid:
                continue
            vals = {h: raw[idx[h]].strip() for h in OWNER_HEADERS if h in idx}
            if pid in owner and not any(vals.values()):
                continue   # a stale duplicate never wipes real entries
            owner[pid] = vals
            full[pid] = {h: raw[i] for h, i in idx.items()}
    return owner, full


# --- row building ---------------------------------------------------------
def build_rows(store: Store, criteria: Criteria, ts: str, owner_states: dict[str, dict]) -> list[dict]:
    fresh = criteria.get("freshness", default={}) or {}
    apply_days = int(fresh.get("apply_soon_days", 7))
    track = criteria.get("tracking", default={}) or {}
    follow_days = int(track.get("follow_up_after_days", 10))
    follow_statuses = set(track.get("follow_up_statuses", ["Applied", "Networking"]))
    now = parse_when(ts)
    rows = []
    for r in store.pipeline_rows():
        try:
            row = _build_row(r, owner_states, now, apply_days, follow_days, follow_statuses,
                             fresh.get("evergreen_flag") or f"Open {fresh.get('evergreen_after_days', 180)}+ days")
        except Exception as exc:     # one malformed posting never blocks the Sheet update
            log.warning("Sheet: skipped %s (%s)", r["posting_id"], exc)
            continue
        if row is not None:
            rows.append(row)
    return rows


def _build_row(r, owner_states, now, apply_days, follow_days, follow_statuses,
               evergreen_label="Open 60+ days") -> dict | None:
    """Sheet cells for one stored posting (None when it should not be shown)."""
    d = json.loads(r["display"] or "{}")
    st = owner_states.get(r["posting_id"], {})
    touched = any(st.get(k) for k in ("interest", "status", "notes")) or \
        (st.get("decision") or "Pending") != "Pending"
    if r["closed_at"] and not touched:
        return None     # closed and never touched: no longer an option, so it leaves To review
    flags = []
    if r["is_new"]:
        flags.append("New")
    posted = r["posted_at"] or r["first_seen"]
    if not r["closed_at"] and posted:
        try:
            when = parse_when(posted)
            if when and now - when <= timedelta(days=apply_days):
                flags.append("Apply soon")
        except ValueError:
            pass
    if st.get("status") in follow_statuses and st.get("status_changed_at"):
        changed = parse_when(st["status_changed_at"])
        if changed and now - changed >= timedelta(days=follow_days):
            flags.append("Follow up")
    flags += json.loads(r["flags"] or "[]")
    if r["evergreen"]:
        flags.append(evergreen_label)
    if r["closed_at"]:
        flags.append("Closed")
    cells = {
        "Decision": st.get("decision") or "Pending", "Interest": st.get("interest") or "",
        "Status": st.get("status") or "", "Notes": st.get("notes") or "",
        "Applied on": _date(st.get("applied_on")),
        "Company": r["company"], "Role": hyperlink(r["url"], r["title"]),
        "Field": d.get("field", ""), "Location": d.get("location", ""), "Salary": d.get("salary", ""),
        "Extra comp": d.get("extra_comp", ""), "Experience ask": d.get("experience", ""),
        "Visa": d.get("visa", ""), "Flags": ", ".join(dict.fromkeys(flags)),
        "Posted": _date(r["posted_at"]), "First seen": _date(r["first_seen"]),
        "Source": d.get("source", ""), "Posting ID": r["posting_id"],
    }
    tier = re.search(r"tier (\d)", cells.get("Location") or "")
    return {"posting_id": r["posting_id"], "closed": bool(r["closed_at"]),
            "first_seen": r["first_seen"], "cells": cells,
            "tier": int(tier.group(1)) if tier else 9, "posted": r["posted_at"] or ""}


def _sort_key(tab: str, row: dict):
    c = row["cells"]
    if tab == APPLICATIONS:
        return (c["Status"] in ENDED, -_interest(c["Interest"]), STATUS_ORDER.get(c["Status"], 6),
                row["closed"], _neg_date(row["first_seen"]))
    # your edge (AI x hospitality / real estate) first, then newest finds (by day), then NYC
    # before tier 2 before tier 3, then newest posting
    return (row["closed"], "Your edge" not in (c.get("Flags") or ""), _neg_date((row["first_seen"] or "")[:10]),
            row.get("tier", 9),
            _neg_date(str(row.get("posted") or "")[:10]))


# --- sync -----------------------------------------------------------------
def decision_tab_requests(meta: dict, existing: dict, store: Store, criteria: Criteria,
                          ts: str) -> tuple[list[dict], dict]:
    sheet_owner, full_rows = read_owner_values(existing)
    for pid, vals in sheet_owner.items():          # the Sheet is the source of truth for your columns
        store.save_owner_state(pid, vals, ts)
    store.commit()
    states = store.owner_states()
    rows = build_rows(store, criteria, ts, states)
    db_ids = {r["posting_id"] for r in rows}
    for pid, vals in sheet_owner.items():          # touched rows that left the DB view are kept
        if pid not in db_ids and any(v for k, v in vals.items() if k != "Decision" or v != "Pending"):
            cells = {h: full_rows[pid].get(h, "") for h in HEADERS}
            rows.append({"posting_id": pid, "closed": True, "first_seen": "", "cells": cells})

    by_tab: dict[str, list[dict]] = {t: [] for t in DECISION_TABS}
    for r in rows:
        by_tab[route(r["cells"])].append(r)
    requests: list[dict] = []
    counts = {}
    for tab in DECISION_TABS:
        tab_rows = sorted(by_tab[tab], key=lambda r: _sort_key(tab, r))
        counts[tab] = len(tab_rows)
        grid = [HEADERS] + [[r["cells"].get(h, "") for h in HEADERS] for r in tab_rows]
        requests += _decision_tab(meta[tab], grid, len(existing.get(tab) or []))
    return requests, {"rows": len(rows), "tabs": counts}


def _decision_tab(m: dict, grid: list[list], existing_len: int) -> list[dict]:
    sid = m["id"]
    # the grid is resized to the rows plus a little room (shrinking deletes the rest); old rows
    # inside that room are blanked
    need_rows = len(grid) + 20
    padded = grid + [[""] * len(HEADERS)] * (min(existing_len, need_rows) - len(grid))
    col = {h: i for i, h in enumerate(HEADERS)}
    reqs: list[dict] = [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": need_rows, "columnCount": max(m.get("cols", 26), len(HEADERS)), "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(v) for v in row]} for row in padded],
                         "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        _validation(sid, col["Decision"], DECISION_OPTIONS, need_rows),
        _validation(sid, col["Interest"], INTEREST_OPTIONS, need_rows),
        _validation(sid, col["Status"], STATUS_OPTIONS, need_rows),
        {"setBasicFilter": {"filter": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": len(grid),
                                                 "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                                                 "startIndex": ID_COL, "endIndex": ID_COL + 1},
                                       "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
    ]
    if not m.get("has_cf"):
        full = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}
        flags, interest = _col_letter(col["Flags"]), _col_letter(col["Interest"])
        rules = [
            (f'=REGEXMATCH(${flags}2,"Closed")', {"textFormat": {"foregroundColor": GRAY}}),
            (f'=REGEXMATCH(${flags}2,"Follow up")', {"backgroundColor": YELLOW}),
            (f'=VALUE(0&${interest}2)>=4', {"backgroundColor": GREEN}),
        ]
        for i, (formula, fmt) in enumerate(rules):
            reqs.append({"addConditionalFormatRule": {"index": i, "rule": {"ranges": [full], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                "format": fmt}}}})
    return reqs


def _validation(sid: int, col: int, options: list[str], rows: int) -> dict:
    return {"setDataValidation": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": rows,
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST",
                               "values": [{"userEnteredValue": o} for o in options]},
                 "showCustomUi": True, "strict": False}}}


# --- the other tabs (rewritten in full each run) ---------------------------
DROPPED, UNPARSED, FUNNEL, HEALTH = "Dropped", "Unparsed alerts", "Funnel", "Board health"
ALL_TABS = DECISION_TABS + [DROPPED, UNPARSED, FUNNEL, HEALTH]


def _simple_tab(meta: dict, tab: str, header: list[str], rows: list[list], existing_len: int) -> list[dict]:
    sid = meta[tab]["id"]
    grid = [header] + rows
    padded = grid            # the grid is resized to fit, so no blank padding is needed
    return [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": len(grid) + 1,       # shrinks too: old rows (and their cells) go away
            "columnCount": max(meta[tab].get("cols", 26), len(header)), "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(v) for v in _pad(r, len(header))]} for r in padded],
                         "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
    ]


def funnel_rows(store: Store) -> list[list]:
    """Three blocks: latest run by source, coverage, run history."""
    runs = store.runs()
    if not runs:
        return [["No runs yet"]]
    last = runs[-1]
    cov = json.loads(last["coverage"] or "{}") if "coverage" in last.keys() else {}
    rows: list[list] = [
        [f"Latest run: {last['started_at'][:16].replace('T', ' ')} UTC · {last['seconds'] or 0:g}s · "
         f"{last['new_passed'] or 0} new options · {cov.get('open_options', '')} open options in total"],
        [],
        ["Source", "Postings seen", "New", "Passed must-haves", "New options", "Dropped", "Top reasons dropped",
         "Last checked (UTC)"],
    ]
    for r in store.latest_source_stats():       # each source's most recent check (some are polled less often)
        reasons = json.loads(r["reasons"] or "{}")
        top = ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:4])
        rows.append([r["source"], r["seen"], r["new"], r["passed"], r["new_passed"], r["dropped"], top,
                     r["started_at"][:16].replace("T", " ")])
    rows += [[], ["Coverage"]]
    polled = cov.get("boards_polled_by_platform") or {}
    rows.append(["Company boards watched", sum(polled.values()),
                 ", ".join(f"{k} {v}" for k, v in sorted(polled.items(), key=lambda kv: -kv[1]))])
    disc = cov.get("discovered_by_platform") or {}
    via = cov.get("discovered_by_source") or {}
    rows.append(["Boards discovered automatically", sum(disc.values()),
                 f"{cov.get('discovered_this_week', 0)} this week · found via "
                 + (", ".join(f"{k} {v}" for k, v in sorted(via.items(), key=lambda kv: -kv[1])) or "—")])
    rows.append(["JSearch searches today", f"{cov.get('jsearch_used_today', 0)} / {cov.get('jsearch_budget', 0)}",
                 f"{cov.get('jsearch_pairs_searched', 0)} of {cov.get('jsearch_pairs_total', 0)} keyword×city pairs covered"])
    rows.append(["The Muse last full sweep", (cov.get("muse_last_sweep") or "never")[:16].replace("T", " ")])
    unread = cov.get("unread_sites") or []
    rows.append(["Career sites we can't read yet (links seen)",
                 ", ".join(f"{d} {n}" for d, n in unread) or "none yet"])
    rows += [[], ["History"], ["Run (UTC)", "Boards polled", "Postings seen", "New", "Passed must-haves",
                               "New options", "Errors"]]
    for r in reversed(runs[-60:]):
        rows.append([r["started_at"][:16].replace("T", " "), r["boards_polled"], r["postings_seen"], r["new"],
                     r["passed"], r["new_passed"] if r["new_passed"] is not None else "", r["errors"]])
    return rows


def other_tab_rows(store: Store, criteria: Criteria, ts: str, unparsed: list[list] | None = None) -> dict:
    days = 7
    cap = int(criteria.get("tracking", "dropped_tab_max_rows", default=10000) or 10000)
    dropped = [[r["company"], hyperlink(r["url"], r["title"]), r["gate"], r["matched"], _date(r["dropped_at"])]
               for r in store.dropped_rows(days, ts)[:cap]]      # newest first; the database keeps them all
    funnel = funnel_rows(store)
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
        FUNNEL: (["Funnel", "", "", "", "", "", ""], funnel),
        HEALTH: (["Company", "Platform", "Last success", "Last error", "Postings (last vs previous)",
                  "Status"], health),
    }


def sync_all(backend: SheetBackend, store: Store, criteria: Criteria, ts: str | None = None,
             unparsed: list[list] | None = None) -> dict:
    """Every tab: one read + one batchUpdate per run."""
    ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = backend.ensure_tabs(ALL_TABS)
    existing = backend.read(ALL_TABS)
    dec_requests, info = decision_tab_requests(meta, existing, store, criteria, ts)
    per_tab: dict[str, list[dict]] = {t: [] for t in ALL_TABS}
    for r in dec_requests:                       # group by tab so unchanged tabs can be skipped
        per_tab[_tab_of(r, meta)].append(r)
    for tab, (header, rows) in other_tab_rows(store, criteria, ts, unparsed).items():
        per_tab[tab] = _simple_tab(meta, tab, header, rows, len(existing.get(tab, [])))
    # A rewrite makes an open Sheet reload and recalculate (it froze on big tabs), so a tab
    # whose content is the same as last time is left alone.
    last = dict(store.conn.execute("SELECT tab, content_hash FROM sheet_tabs").fetchall())
    requests: list[dict] = []
    skipped = []
    for tab in ALL_TABS:
        h = hashlib.sha1(json.dumps(per_tab[tab], sort_keys=True, default=str).encode()).hexdigest()
        if last.get(tab) == h and existing.get(tab):
            skipped.append(tab)
            continue
        requests += per_tab[tab]
        store.conn.execute("INSERT OR REPLACE INTO sheet_tabs (tab, content_hash, written_at) VALUES (?, ?, ?)",
                           (tab, h, ts))
    order = [t for t in ALL_TABS if meta[t].get("index") != ALL_TABS.index(t)]
    for tab in order:                            # keep the tab order: To review first
        requests.append({"updateSheetProperties": {"properties": {"sheetId": meta[tab]["id"],
                                                                  "index": ALL_TABS.index(tab)},
                                                   "fields": "index"}})
    if requests:
        backend.batch_update(requests)
    store.commit()
    info["requests"] = len(requests)
    info["unchanged_tabs"] = skipped
    return info


def _tab_of(req: dict, meta: dict) -> str:
    """Which tab a request writes to (by sheetId)."""
    ids = {m["id"]: t for t, m in meta.items()}
    def find(o):
        if isinstance(o, dict):
            if "sheetId" in o:
                return o["sheetId"]
            o = list(o.values())
        if isinstance(o, list):
            for v in o:
                if isinstance(v, (dict, list)) and (r := find(v)) is not None:
                    return r
        return None
    return ids[find(req)]


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
        reqs = [{"addSheet": {"properties": {"title": t}}} for t in missing]
        # the blank default tab Google creates would otherwise open first
        for default in ("Sheet1", "Feuille 1", "Hoja 1"):
            if default in have and default not in tabs and len(have) + len(missing) > 1:
                vals = self.ss.values_batch_get([f"'{default}'!A1:Z50"]).get("valueRanges", [{}])[0].get("values")
                if not vals:
                    reqs.append({"deleteSheet": {"sheetId": have[default]["properties"]["sheetId"]}})
        if reqs:
            self.ss.batch_update({"requests": reqs})
            return self.ensure_tabs(tabs)
        out = {}
        for t in tabs:
            p = have[t]["properties"]
            gp = p.get("gridProperties", {})
            out[t] = {"id": p["sheetId"], "rows": gp.get("rowCount", 1000), "cols": gp.get("columnCount", 26),
                      "has_cf": bool(have[t].get("conditionalFormats")), "index": p.get("index")}
        return out

    def read(self, tabs: list[str]) -> dict[str, list[list[str]]]:
        res = self.ss.values_batch_get([f"'{t}'!A:Z" for t in tabs])
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
                del tab["grid"][tab["rows"]:]          # like Sheets: shrinking the grid deletes rows
                tab["cols"] = gp.get("columnCount", tab["cols"])
                if "frozenRowCount" in gp:
                    tab["frozen"] = gp["frozenRowCount"]
                if "index" in body["properties"]:
                    tab["index"] = body["properties"]["index"]
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

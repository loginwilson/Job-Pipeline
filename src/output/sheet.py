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
# one pick column: Pass / 1-5 (your Nov 8 choice); what you scan first, the rest to the right
HEADERS = ["Interest", "Status", "Next step", "Due", "Applied on", "Company", "Role", "Location", "Salary",
           "Experience ask", "Flags", "Notes", "Field", "Extra comp", "Visa", "Posted", "First seen", "Source",
           "Posting ID"]
OWNER_HEADERS = ("Decision", "Interest", "Status", "Notes", "Next step", "Due")
ID_COL = HEADERS.index("Posting ID")
DECISION_OPTIONS = ["Pending", "Apply", "Pass"]
INTEREST_OPTIONS = ["1", "2", "3", "4", "5"]
PICK_OPTIONS = ["Pass", "1", "2", "3", "4", "5"]      # the job tabs' Interest column
STATUS_OPTIONS = ["Not started", "Drafting", "Applied", "Networking", "Interviewing", "Offer",
                  "Rejected", "Withdrawn", "No response"]
ENDED = {"Rejected", "Withdrawn", "No response"}
STATUS_ORDER = {"Offer": 0, "Interviewing": 1, "Networking": 2, "Applied": 3, "Drafting": 4,
                "Not started": 5, "": 5, "Rejected": 7, "Withdrawn": 8, "No response": 9}
PASS_WORDS = {"pass", "veto"}
GREEN = {"red": 0.85, "green": 0.94, "blue": 0.83}
YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.75}
GREEN_5 = {"red": 0.66, "green": 0.85, "blue": 0.62}     # Interest 5: darkest
GREEN_4 = {"red": 0.78, "green": 0.91, "blue": 0.74}
GREEN_3 = {"red": 0.87, "green": 0.95, "blue": 0.84}
GREEN_1 = {"red": 0.93, "green": 0.98, "blue": 0.92}     # Interest 1-2, or Apply with no Interest
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


def parse_day(v) -> "date | None":
    """A date you typed (11/12, 11/12/2026, 2026-11-12, Nov 12...) -> date, else None."""
    from datetime import date as _d
    if v is None or str(v).strip() == "":
        return None
    sv = str(v).strip()
    try:
        from dateutil import parser as dparser
        default = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        dt = dparser.parse(sv, default=default, dayfirst=False, fuzzy=True)
        return _d(dt.year, dt.month, dt.day)
    except (ValueError, OverflowError, ImportError):
        return None


def due_cell(v) -> str:
    """Keep a typed date a real date in Sheets (so it sorts and can turn yellow when due)."""
    d = parse_day(v)
    return f"=DATE({d.year},{d.month},{d.day})" if d else (v or "")


def _due_formula(c: str) -> str:
    # due today or overdue (works whether the cell holds a date or text)
    return f'=AND(LEN(${c}2)>0,IFERROR(IF(ISNUMBER(${c}2),${c}2,DATEVALUE(${c}2)),99999)<=TODAY())'


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


def _from_pick(vals: dict) -> dict:
    """Interest column: 'Pass' hides the job, a number (or 'Apply') means you're interested."""
    v = (vals.get("Interest") or "").strip()
    if v.lower() in PASS_WORDS:
        return {**vals, "Decision": "Pass", "Interest": ""}
    if v.lower() in ("apply", "all-in"):
        return {**vals, "Decision": "Apply", "Interest": ""}
    if _interest(v) > 0:
        return {**vals, "Decision": "Apply"}
    return {**vals, "Decision": "Pending", "Interest": ""}


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
def _norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def make_row_resolver(store: Store):
    """(hidden ID, row cells) -> the posting the row really shows, or None if it can't be told."""
    rows = store.conn.execute(
        "SELECT p.posting_id, p.company, p.title, s.display FROM postings p LEFT JOIN scores s USING(posting_id) "
        "WHERE p.merged_into IS NULL").fetchall()
    by_id = {r["posting_id"]: r for r in rows}
    by_name: dict[tuple, list] = {}
    for r in rows:
        by_name.setdefault((_norm(r["company"]), _norm(r["title"])), []).append(r)

    def loc(r) -> str:
        try:
            return _norm(json.loads(r["display"] or "{}").get("location", ""))
        except ValueError:
            return ""

    def resolve(pid: str, cells: dict) -> str | None:
        co, role = _norm(cells.get("Company")), _norm(cells.get("Role"))
        if not co and not role:
            return pid or None
        r = by_id.get(pid)
        if r is not None and _norm(r["company"]) == co and _norm(r["title"]) == role:
            return pid                                   # the usual case: ID and row agree
        cands = by_name.get((co, role), [])
        if len(cands) > 1:
            here = _norm(cells.get("Location"))
            cands = [c for c in cands if loc(c) == here] or cands
        if len(cands) == 1:
            if pid and pid != cands[0]["posting_id"]:
                log.warning("Sheet: row '%s - %s' had another job's ID (sorted?): fixed", cells.get("Company"),
                            cells.get("Role"))
            return cands[0]["posting_id"]
        if r is None and not cands:
            return pid or None                           # not in the database (kept rows): as before
        log.warning("Sheet: can't tell which job row '%s - %s' is; left unchanged", cells.get("Company"),
                    cells.get("Role"))
        return None
    return resolve


def read_owner_values(existing: dict[str, list[list[str]]],
                      known: dict[str, dict] | None = None,
                      resolve=None) -> tuple[dict[str, dict], dict[str, dict]]:
    """Owner cells per Posting ID from the decision tabs (+ full rows, for rows to keep).
    A picked job shows in To review AND My applications: the copy whose cells differ from
    what was last saved is the one you just edited, so it wins."""
    owner: dict[str, dict] = {}
    full: dict[str, dict] = {}
    known = known or {}

    def saved(pid: str) -> dict:
        k = known.get(pid) or {}
        return {"Decision": k.get("decision") or "", "Interest": k.get("interest") or "",
                "Status": k.get("status") or "", "Notes": k.get("notes") or "",
                "Next step": k.get("next_step") or "", "Due": k.get("due") or ""}

    def same(a: dict, b: dict) -> bool:
        return all((a.get(h) or "").strip() == (b.get(h) or "").strip() for h in OWNER_HEADERS
                   if not (h == "Decision" and {(a.get(h) or "Pending"), (b.get(h) or "Pending")} <= {"", "Pending"}))
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
            if resolve is not None:
                # sorting part of the Sheet can leave the hidden ID column behind: trust the
                # row's own Company + Role (+ Location), not the ID, and skip rows we can't place
                pid = resolve(pid, {h: (raw[i] or "") for h, i in idx.items()})
            if not pid:
                continue
            vals = {h: raw[idx[h]].strip() for h in OWNER_HEADERS if h in idx}
            if "Decision" not in idx:
                vals = _from_pick(vals)
            if pid in owner:
                if not any(vals.values()):
                    continue   # a stale duplicate never wipes real entries
                if pid in known and same(vals, saved(pid)) and not same(owner[pid], saved(pid)):
                    continue   # the other copy is the one you edited
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
    companies = {k.strip().lower(): v for k, v in store.company_owner().items()}
    rows = []
    for r in store.pipeline_rows():
        try:
            row = _build_row(r, owner_states, now, apply_days, follow_days, follow_statuses,
                             fresh.get("evergreen_flag") or f"Open {fresh.get('evergreen_after_days', 180)}+ days")
        except Exception as exc:     # one malformed posting never blocks the Sheet update
            log.warning("Sheet: skipped %s (%s)", r["posting_id"], exc)
            continue
        if row is None:
            continue
        if is_program(r["title"]):
            row["cells"]["Flags"] = "Grad program" + (", " + row["cells"]["Flags"] if row["cells"]["Flags"] else "")
        co = companies.get((r["company"] or "").strip().lower()) or {}
        untouched = row["cells"]["Decision"] == "Pending" and not row["cells"]["Interest"] and not row["cells"]["Status"]
        if co.get("status") == "Not for me" and untouched:
            continue                 # you ruled the company out on the Companies tab
        if co.get("status") in TARGET_STATUSES or _interest(co.get("interest") or "") >= 4:
            flags = row["cells"]["Flags"]
            row["cells"]["Flags"] = "Target company" + (", " + flags if flags else "")
        rows.append(row)
    return rows


def _pick_cell(st: dict) -> str:
    d = (st.get("decision") or "").strip().lower()
    if d in PASS_WORDS:
        return "Pass"
    if st.get("interest"):
        return st["interest"]
    return "3" if d in ("apply", "all-in") else ""      # an Apply with no Interest becomes a 3


def _build_row(r, owner_states, now, apply_days, follow_days, follow_statuses,
               evergreen_label="Open 60+ days") -> dict | None:
    """Sheet cells for one stored posting (None when it should not be shown)."""
    d = json.loads(r["display"] or "{}")
    st = owner_states.get(r["posting_id"], {})
    touched = any(st.get(k) for k in ("interest", "status", "notes", "next_step", "due")) or \
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
    if not r["passed"]:
        # you marked it, so it stays, but say why today's rules would leave it out
        flags.append(f"Outside your rules now ({r['gate'] or 'changed'})")
    if r["evergreen"]:
        flags.append(evergreen_label)
    if r["closed_at"]:
        flags.append("Closed")
    cells = {
        "Decision": st.get("decision") or "Pending", "Interest": _pick_cell(st),
        "Status": st.get("status") or "", "Notes": st.get("notes") or "",
        "Applied on": _date(st.get("applied_on")),
        "Next step": st.get("next_step") or "", "Due": due_cell(st.get("due")),
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


def _preference_rank(flags: str) -> int:
    """Your ranking (Nov 8): target companies, AI x hospitality / real estate, hospitality /
    real estate, an AI-forward company, the rest, then stretches."""
    if "Target company" in flags:
        return 0                   # a company you picked on the Companies tab
    if "Stretch" in flags:
        return 5                   # reaches last, after every realistic fit
    for rank, mark in enumerate(("Your edge", "Preferred field", "AI-forward company"), start=1):
        if mark in flags:
            return rank
    return 4


def _sort_key(tab: str, row: dict):
    c = row["cells"]
    if tab == APPLICATIONS:
        return (c["Status"] in ENDED, -_interest(c["Interest"]), STATUS_ORDER.get(c["Status"], 6),
                row["closed"], _neg_date(row["first_seen"]))
    # your edge (AI x hospitality / real estate) first, then newest finds (by day), then NYC
    # before tier 2 before tier 3, then newest posting
    return (row["closed"], _preference_rank(c.get("Flags") or ""), _neg_date((row["first_seen"] or "")[:10]),
            row.get("tier", 9),
            _neg_date(str(row.get("posted") or "")[:10]))


# --- sync -----------------------------------------------------------------
def decision_tab_requests(meta: dict, existing: dict, store: Store, criteria: Criteria,
                          ts: str) -> tuple[list[dict], dict]:
    sheet_owner, full_rows = read_owner_values(existing, store.owner_states(), make_row_resolver(store))
    for pid, vals in sheet_owner.items():          # the Sheet is the source of truth for your columns
        store.save_owner_state(pid, vals, ts)
    store.commit()
    states = store.owner_states()
    rows = build_rows(store, criteria, ts, states)
    db_ids = {r["posting_id"] for r in rows}
    for pid, vals in sheet_owner.items():          # touched rows that left the DB view are kept
        if pid not in db_ids and any(v for k, v in vals.items() if k != "Decision" or v != "Pending"):
            cells = {h: full_rows[pid].get(h, "") for h in HEADERS}
            known = store.conn.execute("SELECT title, url FROM postings WHERE posting_id=?", (pid,)).fetchone()
            if known and known["url"]:
                cells["Role"] = hyperlink(known["url"], known["title"] or cells.get("Role", ""))
            rows.append({"posting_id": pid, "closed": True, "first_seen": "", "cells": cells})

    by_tab: dict[str, list[dict]] = {t: [] for t in DECISION_TABS}
    for r in rows:
        tab = route(r["cells"])
        by_tab[tab].append(r)
        if tab == APPLICATIONS:
            by_tab[REVIEW].append(r)      # your picks also stay in To review, shaded by Interest
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
        {"setDataValidation": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": need_rows,
                                         "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}},
        _validation(sid, col["Interest"], PICK_OPTIONS, need_rows),
        _validation(sid, col["Status"], STATUS_OPTIONS, need_rows),
        {"setBasicFilter": {"filter": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": len(grid),
                                                 "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                                                 "startIndex": ID_COL, "endIndex": ID_COL + 1},
                                       "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
    ]
    full = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}
    flags, interest = _col_letter(col["Flags"]), _col_letter(col["Interest"])
    # first matching rule wins: closed grey, follow-up yellow, then your picks in greens by Interest
    rules = [
        (f'=REGEXMATCH(${flags}2,"Closed")', {"textFormat": {"foregroundColor": GRAY}}),
        (f'=REGEXMATCH(${flags}2,"Follow up")', {"backgroundColor": YELLOW}),
        (_due_formula(_col_letter(col["Due"])), {"backgroundColor": YELLOW}),
        (f'=VALUE(0&${interest}2)=5', {"backgroundColor": GREEN_5}),
        (f'=VALUE(0&${interest}2)=4', {"backgroundColor": GREEN_4}),
        (f'=VALUE(0&${interest}2)=3', {"backgroundColor": GREEN_3}),
        (f'=IFERROR(VALUE(0&${interest}2)>=1,FALSE)', {"backgroundColor": GREEN_1}),
    ]
    if m.get("cf_formulas") != [f for f, _ in rules]:
        for _ in range(int(m.get("cf_count", 1 if m.get("has_cf") else 0))):
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}})
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
COMPANIES, COMPANY_JOBS, PROGRAMS = "Companies", "Company jobs", "Grad programs"
HOME, CONTACTS_TAB = "Home", "Contacts"
BACK_OFFICE = [PASSED, DROPPED, UNPARSED, FUNNEL, HEALTH]
# your hub first, then the inboxes you browse, then the back office
ALL_TABS = [HOME, COMPANIES, CONTACTS_TAB, APPLICATIONS, REVIEW, PROGRAMS, COMPANY_JOBS, PASSED,
            DROPPED, UNPARSED, FUNNEL, HEALTH]

# --- grad programs: their own view with lighter rules ---------------------------
PROGRAM_RX = re.compile(
    r"\b(?:new grad(?:uate)?s?|graduate (?:program|leadership|scheme|rotational|associate)|grad program|"
    r"university (?:hire|graduate|grad)|campus (?:hire|full[- ]time)|rotational|rotation program|"
    r"(?:leadership|career|operations|management|business|analyst|associate) development program|"
    r"leadership program|early[- ]career|emerging talent|class of 202[6-8]|202[78] (?:start|graduates?|new grad)|"
    r"wolf graduate|associates? program|analyst program|fellowship)\b", re.I)
PROGRAM_HEADERS = ["Add", "Company", "Program", "Where it stands", "Location", "Salary", "Experience ask",
                   "Posting ID"]
# a program is hidden only for these: things you can't change (skills, degree, citizenship,
# internship, starting before you graduate); pay, title, location and stated years are notes
_PROGRAM_BLOCKERS = {"technical", "credentials", "work_authorization", "start_date", "employment_type",
                     "specialist", "seniority", "not_related_to_degree", "no_description", "not_a_real_opening"}
_PROGRAM_FIELD_BLOCKERS = ("tech", "design", "product", "banking markets", "legal", "accounting", "hr people",
                           "pure data", "investment banking")


_PROGRAM_HARD_TITLE = re.compile(r"\b(?:full[- ]stack|software|developer|engineer(?:ing)?|technician|data scientist|"
                                 r"machine learning|quant|trader|designer)\b", re.I)
_ABROAD = re.compile(r"\b(?:United Kingdom|England|London|Korea|Seoul|Hong Kong|Singapore|India|Germany|France|"
                     r"Japan|China|Australia|Brazil|Brasil|Mexico|Ireland|Netherlands|Amsterdam|Spain|Israel|Poland|"
                     r"Dubai|UAE|Switzerland|Sweden|Italy|Philippines|Taiwan)\b", re.I)


def is_program(title: str) -> bool:
    return bool(PROGRAM_RX.search(title or ""))


def program_rows(store: Store) -> list[list]:
    tracked = {r["posting_id"] for r in store.pipeline_rows()}
    out = []
    for r in store.conn.execute(
            "SELECT p.posting_id, p.company, p.title, p.url, s.passed, s.failures, s.display FROM postings p "
            "JOIN scores s USING(posting_id) WHERE p.closed_at IS NULL AND p.merged_into IS NULL"):
        if not is_program(r["title"]):
            continue
        d = json.loads(r["display"] or "{}")
        fails = json.loads(r["failures"] or "[]")
        if r["posting_id"] in tracked:
            where = "✅ In To review" if r["passed"] else "✅ Your pick (in My applications)"
            rank = 0
        else:
            if any(f.get("gate") in _PROGRAM_BLOCKERS for f in fails):
                continue
            if any(f.get("gate") == "field" and str(f.get("matched") or "").startswith(_PROGRAM_FIELD_BLOCKERS)
                   for f in fails):
                continue
            if _PROGRAM_HARD_TITLE.search(r["title"] or ""):
                continue          # engineering / technician programs
            if any(f.get("gate") == "location" and _ABROAD.search(str(f.get("matched") or "")) for f in fails):
                continue          # outside the US and Canada
            notes = "; ".join(f"{_GATE_WORDS.get(f.get('gate'), f.get('gate'))}: {str(f.get('matched') or '')[:70]}"
                              for f in fails)
            where = "Outside your usual rules: " + notes
            rank = 1
        out.append((rank, (r["company"] or "").lower(), ["", r["company"], hyperlink(r["url"], r["title"]), where,
                                                          d.get("location", ""), d.get("salary", ""),
                                                          d.get("experience", ""), r["posting_id"]]))
    return [row for _, _, row in sorted(out, key=lambda x: (x[0], x[1], str(x[2][2])))]


def _programs_tab(meta: dict, rows: list[list], existing_len: int) -> list[dict]:
    m = meta[PROGRAMS]
    sid = m["id"]
    grid = [PROGRAM_HEADERS] + rows
    need = len(grid) + 20
    padded = grid + [[""] * len(PROGRAM_HEADERS)] * (min(existing_len, need) - len(grid))
    return [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": need, "columnCount": max(m.get("cols", 26), len(PROGRAM_HEADERS)), "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(v) for v in _pad(r, len(PROGRAM_HEADERS))]} for r in padded],
                         "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        _validation(sid, 0, ["Add"], need),
    ]
COMPANY_JOB_HEADERS = ["Add", "Company", "Role", "Passes your rules?", "Location", "Salary", "Experience ask",
                       "Posting ID"]
_GATE_WORDS = {"field": "not your field", "experience": "experience", "salary": "pay below your minimum",
               "location": "location", "technical": "technical skill", "credentials": "credential / degree",
               "seniority": "too senior", "employment_type": "job type", "start_date": "start date",
               "work_authorization": "work authorization", "specialist": "specialist job",
               "no_description": "no description", "not_related_to_degree": "not related to your degree (OPT)",
               "not_a_real_opening": "not a real opening"}


def read_company_job_adds(existing: dict) -> list[str]:
    """Posting IDs you marked 'Add' on the Company jobs or Grad programs tab."""
    out = []
    for tab in (COMPANY_JOBS, PROGRAMS):
        rows = existing.get(tab) or []
        if not rows:
            continue
        idx = {h: i for i, h in enumerate(rows[0])}
        if "Add" not in idx or "Posting ID" not in idx:
            continue
        role_col = "Role" if "Role" in idx else "Program"
        for raw in rows[1:]:
            raw = _pad(raw, len(rows[0]))
            if (raw[idx["Add"]] or "").strip().lower() == "add" and raw[idx["Posting ID"]]:
                cells = {h: (raw[i] or "") for h, i in idx.items()}
                cells["Role"] = cells.get(role_col, "")
                out.append((raw[idx["Posting ID"]].strip(), cells))
    return out


def company_job_rows(store: Store, cap: int = 1500) -> list[list]:
    """Every open job at a company you marked Target-type (or Interest 4-5) that isn't already
    on your job tabs - with why the rules left it out. Pick Add to put it in My applications."""
    owner = store.company_owner()
    picked = {c.strip().lower() for c, o in owner.items()
              if (o.get("status") or "") in TARGET_STATUSES or _interest(o.get("interest") or "") >= 4}
    out = []
    for r in store.company_job_rows(picked):
        d = json.loads(r["display"] or "{}")
        why = (_GATE_WORDS.get(r["gate"] or "", r["gate"] or "not checked yet")
               + (f": {str(r['matched'] or '')[:90]}" if r["matched"] else ""))
        out.append(["", r["company"], hyperlink(r["url"], r["title"]), "No: " + why,
                    d.get("location", ""), d.get("salary", ""), d.get("experience", ""), r["posting_id"]])
    # closest misses first: experience / pay / location before field and technical drops
    order = {"experience": 0, "salary": 1, "location": 2, "start_date": 3, "credentials": 4}
    by_id = {r["posting_id"]: r for r in store.company_job_rows(picked)}
    out.sort(key=lambda row: (row[1].lower(), order.get(by_id[row[7]]["gate"] or "", 9), str(row[2])))
    return out[:cap]


def _company_jobs_tab(meta: dict, rows: list[list], existing_len: int) -> list[dict]:
    m = meta[COMPANY_JOBS]
    sid = m["id"]
    grid = [COMPANY_JOB_HEADERS] + rows
    need = len(grid) + 20
    padded = grid + [[""] * len(COMPANY_JOB_HEADERS)] * (min(existing_len, need) - len(grid))
    return [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": need, "columnCount": max(m.get("cols", 26), len(COMPANY_JOB_HEADERS)), "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(v) for v in _pad(r, len(COMPANY_JOB_HEADERS))]} for r in padded],
                         "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        _validation(sid, 0, ["Add"], need),
    ]

# --- pipeline 2: the Companies tab (your hub) ---------------------------------
COMPANY_HEADERS = ["Interest", "Status", "Company", "Fit", "Next step", "Due", "Connections", "Last contact",
                   "Your picks", "Roles that fit you", "Grad programs", "A role that fits", "Open roles",
                   "What they do", "Careers page", "Contact", "Notes", "Source", "First seen"]
COMPANY_OWNER = ("Interest", "Status", "Next step", "Due", "Contact", "Notes")
# your relationship with the company, from first look to offer
COMPANY_STATUS = ["Researching", "Target", "Networking", "Coffee chat", "Referred", "Applied", "Interviewing",
                  "Offer", "Watching", "Not for me"]
_COMPANY_STAGE = {s: i for i, s in enumerate(["Offer", "Interviewing", "Applied", "Referred", "Coffee chat",
                                               "Networking", "Talking", "Reached out", "Target"])}
TARGET_STATUSES = {"Target", "Networking", "Coffee chat", "Referred", "Applied", "Interviewing", "Offer",
                   "Reached out", "Talking"}       # last two: earlier names, still honoured
_FIT_RANK = {"AI × hospitality": 0, "AI × real estate": 0, "hospitality": 1, "real estate": 1, "AI-forward": 2}

# --- Contacts tab: yours entirely (the pipeline reads it, never rewrites your rows) ---
CONTACTS = "Contacts"
CONTACT_HEADERS = ["Company", "Name", "Role / team", "How you're connected", "LinkedIn", "Status",
                   "Last contact", "Next step", "Due", "Notes"]
CONTACT_STATUS = ["To reach out", "Messaged", "Replied", "Coffee chat scheduled", "Coffee chat done",
                  "Referred", "Thank-you sent", "Keep in touch", "No reply"]
CONTACT_NUDGE_DAYS = 7      # Messaged with no reply for this long -> follow up


def read_contacts(existing: dict) -> list[dict]:
    rows = existing.get(CONTACTS) or []
    if not rows:
        return []
    idx = {h: i for i, h in enumerate(rows[0])}
    out = []
    for raw in rows[1:]:
        raw = _pad(raw, len(rows[0]))
        c = {h: (raw[idx[h]] or "").strip() for h in CONTACT_HEADERS if h in idx}
        if c.get("Company") or c.get("Name"):
            out.append(c)
    return out


def _contacts_tab(meta: dict, existing_len: int) -> list[dict]:
    """Header, dropdown and colours only - your rows are never touched."""
    m = meta[CONTACTS]
    sid = m["id"]
    col = {h: i for i, h in enumerate(CONTACT_HEADERS)}
    reqs = [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": max(m.get("rows", 1000), 500), "columnCount": max(m.get("cols", 26), len(CONTACT_HEADERS)),
            "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(h) for h in CONTACT_HEADERS]}], "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        _validation(sid, col["Status"], CONTACT_STATUS, max(m.get("rows", 1000), 500)),
    ]
    status, last, due = (_col_letter(col[h]) for h in ("Status", "Last contact", "Due"))
    rules = [
        (_due_formula(due), {"backgroundColor": YELLOW}),
        (f'=AND(${status}2="Messaged",LEN(${last}2)>0,TODAY()-IFERROR(IF(ISNUMBER(${last}2),${last}2,'
         f'DATEVALUE(${last}2)),TODAY())>={CONTACT_NUDGE_DAYS})', {"backgroundColor": YELLOW}),
        (f'=OR(${status}2="Coffee chat done",${status}2="Referred")', {"backgroundColor": GREEN_4}),
    ]
    if m.get("cf_formulas") != [f for f, _ in rules]:
        full = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(CONTACT_HEADERS)}
        for _ in range(int(m.get("cf_count", 0))):
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}})
        for i, (formula, fmt) in enumerate(rules):
            reqs.append({"addConditionalFormatRule": {"index": i, "rule": {"ranges": [full], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                "format": fmt}}}})
    return reqs


def read_company_owner(existing: dict) -> dict[str, dict]:
    rows = existing.get(COMPANIES) or []
    if not rows:
        return {}
    idx = {h: i for i, h in enumerate(rows[0])}
    if "Company" not in idx:
        return {}
    out = {}
    for raw in rows[1:]:
        raw = _pad(raw, len(rows[0]))
        name = (raw[idx["Company"]] or "").strip()
        if name:
            out[name] = {h: (raw[idx[h]] or "").strip() for h in COMPANY_OWNER if h in idx}
    return out


def home_grid(store: Store, contacts: list[dict], crows: list[list], ts: str) -> tuple[list, list[list]]:
    """One screen: what to do, where you stand, the companies that stand out, what's new."""
    today = parse_when(ts).date()
    week = today + timedelta(days=7)
    W = 6
    rows: list[list] = []
    def section(title, header=None):
        rows.append([""] * W)
        rows.append([title] + [""] * (W - 1))
        if header:
            rows.append(header + [""] * (W - len(header)))

    # --- to do ---
    todo = []           # (sort day, due text, what, where, from)
    for r in store.conn.execute(
            "SELECT o.next_step, o.due, o.status, o.status_changed_at, p.company, p.title FROM owner_state o "
            "JOIN postings p USING(posting_id)"):
        d = parse_day(r["due"])
        if r["next_step"] or d:
            todo.append((d or week, d, r["next_step"] or "(next step)", f"{r['company']} - {r['title']}", "My applications"))
        if (r["status"] or "") == "Applied" and r["status_changed_at"]:
            changed = parse_when(r["status_changed_at"]).date()
            if (today - changed).days >= 10:
                todo.append((today, None, f"Follow up on your application (applied {changed.isoformat()})",
                             f"{r['company']} - {r['title']}", "My applications"))
    for name, o in store.company_owner().items():
        d = parse_day(o.get("due"))
        if o.get("next_step") or d:
            todo.append((d or week, d, o.get("next_step") or "(next step)", name, "Companies"))
    for c in contacts:
        d = parse_day(c.get("Due"))
        who = f"{c.get('Name') or '?'} @ {c.get('Company') or '?'}"
        if c.get("Next step") or d:
            todo.append((d or week, d, c.get("Next step") or "(next step)", who, "Contacts"))
        last = parse_day(c.get("Last contact"))
        if c.get("Status") == "Messaged" and last and (today - last).days >= CONTACT_NUDGE_DAYS:
            todo.append((today, None, f"No reply since {last.isoformat()} - nudge or try someone else", who, "Contacts"))
    todo = sorted([t for t in todo if t[0] <= week], key=lambda t: (t[0], t[2]))[:30]
    section("TO DO  (overdue and due this week first)", ["Due", "What", "Where", "From"])
    for day, d, what, where, frm in todo:
        flag = "OVERDUE " if d and d < today else ("TODAY " if d == today else "")
        rows.append([flag + (d.isoformat() if d else "now"), what, where, frm, "", ""])
    if not todo:
        rows.append(["", "Nothing due. Add a Next step + Due on a job, company or contact to see it here.", "", "", "", ""])

    # --- where you stand ---
    jobs = {"Shortlisted": 0, "Preparing": 0, "Applied": 0, "Interviewing": 0, "Offer": 0, "Closed": 0}
    for r in store.conn.execute("SELECT decision, interest, status FROM owner_state"):
        st, dec = (r["status"] or ""), (r["decision"] or "")
        if dec.lower() in PASS_WORDS:
            continue
        if st in ("Offer",):
            jobs["Offer"] += 1
        elif st == "Interviewing":
            jobs["Interviewing"] += 1
        elif st == "Applied":
            jobs["Applied"] += 1
        elif st in ENDED:
            jobs["Closed"] += 1
        elif st in ("Drafting", "Networking"):
            jobs["Preparing"] += 1
        elif dec.lower() in ("apply", "all-in") or r["interest"]:
            jobs["Shortlisted"] += 1
    comp = {}
    for o in store.company_owner().values():
        if o.get("status"):
            comp[o["status"]] = comp.get(o["status"], 0) + 1
    ppl = {}
    for c in contacts:
        k = c.get("Status") or "To reach out"
        ppl[k] = ppl.get(k, 0) + 1
    section("WHERE YOU STAND")
    rows.append(["Jobs"] + [" · ".join(f"{k} {v}" for k, v in jobs.items())] + [""] * (W - 2))
    rows.append(["Companies"] + [" · ".join(f"{k} {comp[k]}" for k in COMPANY_STATUS if comp.get(k)) or "none marked yet"]
                + [""] * (W - 2))
    rows.append(["Contacts"] + [" · ".join(f"{k} {ppl[k]}" for k in CONTACT_STATUS if ppl.get(k))
                                or "none yet - add people on the Contacts tab"] + [""] * (W - 2))

    # --- standout companies ---
    ci = {h: i for i, h in enumerate(COMPANY_HEADERS)}
    standout = [r for r in crows if r[ci["Status"]] in TARGET_STATUSES or _interest(r[ci["Interest"]]) >= 3][:15]
    section("STANDOUT COMPANIES", ["Company", "Status", "Interest", "Next step", "Connections",
                                   "Your picks · roles that fit · programs"])
    for r in standout:
        rows.append([r[ci["Company"]], r[ci["Status"]], r[ci["Interest"]], r[ci["Next step"]], r[ci["Connections"]],
                     f"{r[ci['Your picks']] or 0} · {r[ci['Roles that fit you']] or 0} · {r[ci['Grad programs']] or 0}"])
    if not standout:
        rows.append(["", "Mark companies Target (or Interest 3+) on the Companies tab to see them here.", "", "", "", ""])

    # --- new since yesterday ---
    since = (parse_when(ts) - timedelta(hours=36)).isoformat(timespec="seconds")
    targets = {c.strip().lower() for c, o in store.company_owner().items()
               if (o.get("status") or "") in TARGET_STATUSES or _interest(o.get("interest") or "") >= 4}
    new = []
    for r in store.conn.execute(
            "SELECT p.company, p.title, p.url, s.passed, s.flags FROM postings p JOIN scores s USING(posting_id) "
            "WHERE p.first_seen >= ? AND p.closed_at IS NULL AND p.merged_into IS NULL", (since,)):
        flags = ", ".join(f for f in json.loads(r["flags"] or "[]") if f.startswith(("Your edge", "Preferred", "Stretch")))
        if not r["passed"]:
            continue          # only finds that fit you (everything else is on Company jobs)
        kind = ("Target company" if (r["company"] or "").strip().lower() in targets else
                "Grad program" if is_program(r["title"]) else
                "Your edge" if "Your edge" in flags else None)
        if kind:
            new.append((kind, r["company"], hyperlink(r["url"], r["title"]), flags))
    order = {"Target company": 0, "Your edge": 1, "Grad program": 2}
    section("NEW SINCE YESTERDAY", ["Kind", "Company", "Role", "Flags"])
    for k, c, role, f in sorted(new, key=lambda x: (order[x[0]], x[1]))[:25]:
        rows.append([k, c, role, f, "", ""])
    if not new:
        rows.append(["", "Nothing new at your targets, edge or programs in the last day.", "", "", "", ""])

    header = [f"Job hunt - home ({today.isoformat()})", "", "", "", "", ""]
    return header, rows


def _company_rollups(store: Store, contacts: list[dict]) -> dict[str, dict]:
    """Per company (lower-cased): your connections, last contact, picks and grad programs."""
    out: dict[str, dict] = {}
    def get(name):
        return out.setdefault((name or "").strip().lower(), {"people": [], "last": None, "picks": 0, "programs": 0})
    for c in contacts:
        g = get(c.get("Company"))
        g["people"].append(f"{c.get('Name') or '?'} ({c.get('Status') or 'to reach out'})")
        d = parse_day(c.get("Last contact"))
        if d and (g["last"] is None or d > g["last"]):
            g["last"] = d
    for r in store.conn.execute(
            "SELECT p.company FROM postings p JOIN owner_state o USING(posting_id) WHERE "
            "COALESCE(o.decision,'') NOT IN ('', 'Pending', 'Pass') OR COALESCE(o.interest,'')<>''"):
        get(r["company"])["picks"] += 1
    for row in program_rows(store):
        get(row[1])["programs"] += 1
    return out


def company_rows(store: Store, seeds: list[dict], ts: str, contacts: list[dict] | None = None) -> list[list]:
    store.clear_auto_companies_v1()
    for c in seeds:                                   # your list / researched companies
        store.note_target_company(c.get("name", ""), c.get("fit", ""), c.get("about", ""),
                                  c.get("source", "your list"), ts, c.get("careers"))
    owner = store.company_owner()
    posts = store.company_postings()
    roll = _company_rollups(store, contacts or [])
    out = []
    for r in store.target_companies():
        o = owner.get(r["company"]) or {}
        k = r["company"].strip().lower()
        p = posts.get(k) or {"open": 0, "fitting": 0, "first": None}
        g = roll.get(k) or {"people": [], "last": None, "picks": 0, "programs": 0}
        first = hyperlink(p["first"][0], p["first"][1]) if p["first"] else ""
        careers = hyperlink(r["careers_url"], "careers") if r["careers_url"] else ""
        people = f"{len(g['people'])}: " + ", ".join(g["people"][:3]) if g["people"] else ""
        out.append([o.get("interest") or "", o.get("status") or "", r["company"], r["fit"] or "",
                    o.get("next_step") or "", due_cell(o.get("due")), people,
                    g["last"].isoformat() if g["last"] else "", g["picks"] or "", p["fitting"] or "",
                    g["programs"] or "", first, p["open"] or "", r["about"] or "", careers, o.get("contact") or "",
                    o.get("notes") or "", r["source"] or "", _date(r["first_seen"])])
    def key(row):
        status = row[1]
        return (status == "Not for me", _COMPANY_STAGE.get(status, 99), -_interest(row[0]),
                -(row[8] or 0), _FIT_RANK.get(row[3], 9), -(row[9] or 0), row[2].lower())
    return sorted(out, key=key)


def _company_tab(meta: dict, rows: list[list], existing_len: int) -> list[dict]:
    m = meta[COMPANIES]
    sid = m["id"]
    grid = [COMPANY_HEADERS] + rows
    need = len(grid) + 20
    padded = grid + [[""] * len(COMPANY_HEADERS)] * (min(existing_len, need) - len(grid))
    col = {h: i for i, h in enumerate(COMPANY_HEADERS)}
    return [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": need, "columnCount": max(m.get("cols", 26), len(COMPANY_HEADERS)), "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(v) for v in _pad(r, len(COMPANY_HEADERS))]} for r in padded],
                         "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        _validation(sid, col["Interest"], INTEREST_OPTIONS, need),
        _validation(sid, col["Status"], COMPANY_STATUS, need),
    ] + _company_colours(m, sid, col)


def _company_colours(m: dict, sid: int, col: dict) -> list[dict]:
    st, it, due = _col_letter(col["Status"]), _col_letter(col["Interest"]), _col_letter(col["Due"])
    rules = [
        (f'=${st}2="Not for me"', {"textFormat": {"foregroundColor": GRAY}}),
        (_due_formula(due), {"backgroundColor": YELLOW}),
        (f'=OR(${st}2="Offer",${st}2="Interviewing",${st}2="Referred",VALUE(0&${it}2)=5)', {"backgroundColor": GREEN_5}),
        (f'=OR(${st}2="Coffee chat",${st}2="Applied",VALUE(0&${it}2)=4)', {"backgroundColor": GREEN_4}),
        (f'=OR(${st}2="Networking",${st}2="Target",VALUE(0&${it}2)=3)', {"backgroundColor": GREEN_3}),
    ]
    if m.get("cf_formulas") == [f for f, _ in rules]:
        return []
    full = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(COMPANY_HEADERS)}
    reqs = [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}} for _ in range(int(m.get("cf_count", 0)))]
    for i, (formula, fmt) in enumerate(rules):
        reqs.append({"addConditionalFormatRule": {"index": i, "rule": {"ranges": [full], "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]}, "format": fmt}}}})
    return reqs


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
    known_companies = {r["company"].strip().lower() for r in store.target_companies()}
    for name, vals in read_company_owner(existing).items():   # your Companies-tab columns first
        store.save_company_owner(name, vals, ts)
        if name.strip().lower() not in known_companies:        # a company you typed in yourself
            store.note_target_company(name.strip(), "", "", "you added on the Sheet", ts)
    resolve = make_row_resolver(store)
    for pid, cells in read_company_job_adds(existing):         # jobs you added from Company jobs
        pid = resolve(pid, cells)
        if pid:
            store.add_forced(pid, ts)
    for pid in load_picked_jobs():                             # jobs you asked to add directly
        if store.conn.execute("SELECT 1 FROM postings WHERE posting_id=?", (pid,)).fetchone():
            store.add_forced(pid, ts)
    store.commit()
    dec_requests, info = decision_tab_requests(meta, existing, store, criteria, ts)
    per_tab: dict[str, list[dict]] = {t: [] for t in ALL_TABS}
    for r in dec_requests:                       # group by tab so unchanged tabs can be skipped
        per_tab[_tab_of(r, meta)].append(r)
    for tab, (header, rows) in other_tab_rows(store, criteria, ts, unparsed).items():
        per_tab[tab] = _simple_tab(meta, tab, header, rows, len(existing.get(tab, [])))
    contacts = read_contacts(existing)
    crows = company_rows(store, load_target_companies(), ts, contacts)
    per_tab[CONTACTS] = _contacts_tab(meta, len(existing.get(CONTACTS, [])))
    per_tab[HOME] = _simple_tab(meta, HOME, *home_grid(store, contacts, crows, ts), 0)
    per_tab[COMPANIES] = _company_tab(meta, crows, len(existing.get(COMPANIES, [])))
    per_tab[COMPANY_JOBS] = _company_jobs_tab(meta, company_job_rows(store), len(existing.get(COMPANY_JOBS, [])))
    prows = program_rows(store)
    per_tab[PROGRAMS] = _programs_tab(meta, prows, len(existing.get(PROGRAMS, [])))
    info["programs"] = len(prows)
    info["companies"] = len(crows)
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
    for tab in order:                            # keep the tab order: Home first
        requests.append({"updateSheetProperties": {"properties": {"sheetId": meta[tab]["id"],
                                                                  "index": ALL_TABS.index(tab)},
                                                   "fields": "index"}})
    # keep it simple: back-office tabs are hidden once (unhide any time from the Sheets menu)
    if not store.conn.execute("SELECT 1 FROM sheet_tabs WHERE tab='__hidden_v1'").fetchone():
        for tab in BACK_OFFICE:
            requests.append({"updateSheetProperties": {"properties": {"sheetId": meta[tab]["id"], "hidden": True},
                                                       "fields": "hidden"}})
        store.conn.execute("INSERT OR REPLACE INTO sheet_tabs (tab, content_hash, written_at) "
                           "VALUES ('__hidden_v1', '', ?)", (ts,))
    if requests:
        backend.batch_update(requests)
    store.commit()
    info["requests"] = len(requests)
    info["unchanged_tabs"] = skipped
    return info


def load_picked_jobs() -> list[str]:
    """config/picked_jobs.yaml: posting IDs you asked to add straight to My applications."""
    from ..config import CONFIG_DIR, load_yaml
    path = CONFIG_DIR / "picked_jobs.yaml"
    if not path.exists():
        return []
    return [str(j.get("id")) for j in ((load_yaml(path) or {}).get("jobs") or []) if j.get("id")]


def load_target_companies() -> list[dict]:
    """config/target_companies.yaml: companies you or research added (pipeline 2 seeds)."""
    from ..config import CONFIG_DIR, load_yaml
    path = CONFIG_DIR / "target_companies.yaml"
    if not path.exists():
        return []
    return list((load_yaml(path) or {}).get("companies") or [])


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
                      "has_cf": bool(have[t].get("conditionalFormats")), "index": p.get("index"),
                      "cf_count": len(have[t].get("conditionalFormats") or []),
                      "cf_formulas": [((((cf.get("booleanRule") or {}).get("condition") or {}).get("values")
                                        or [{}])[0]).get("userEnteredValue")
                                      for cf in have[t].get("conditionalFormats") or []]}
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
                    "has_cf": bool(self.tabs[t]["cf"]), "cf_count": len(self.tabs[t]["cf"]),
                    "cf_formulas": [c["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
                                    for c in self.tabs[t]["cf"]]} for t in tabs}

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
            elif kind == "deleteConditionalFormatRule":
                cf = self._tab_by_id(body["sheetId"])["cf"]
                if cf:
                    cf.pop(body["index"])
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
        while len(grid) <= row:          # typing into an empty row below the data
            grid.append([])
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

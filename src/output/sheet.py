"""Google Sheet sync - the Sheet is a view of SQLite; your columns are yours.

Four tabs (Nov 9: you type in ONE place; everything else follows instantly):
  To review  every job, once. Score it (Pass / 1-5) and track it (Status, Next step, Due,
             Notes). The hourly run only adds new postings here ("Applied on" fills itself).
  Apply      a live formula view: your 1-5s, best first, finished applications last.
  Passed     a live formula view of your passes.
  Contacts   yours: people at the companies on Apply (Nov 11, 16 columns: Company, Job, Name, Role / team,
             Location, LinkedIn, How you're connected, Priority, Why, Angle, Message, Status,
             Last contact, Next step, Due, Notes).
  (+ hidden back office: Dropped, Unparsed alerts, Funnel, Board health)

Your cells are read from To review by Company + Role (+ Location), saved in the database,
and never overwritten. Per run: ONE read + ONE spreadsheets.batchUpdate.
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

JOBS, PICKS, PASSED = "To review", "Apply", "Passed"
RENAMES = {"Jobs": JOBS, "My picks": PICKS}                    # tabs from the first one-table layout
REVIEW = JOBS
# To review: the one table you type in (score + tracking), the posting details to the right
HEADERS = ["Interest", "Status", "Next step", "Due", "Notes", "Company", "Role", "Location", "Salary",
           "Experience ask", "Flags", "Field", "Extra comp", "Visa", "Posted", "First seen", "Source",
           "Applied on", "Link", "Posting ID"]
# Apply: a live, sorted view of your 1-5s (a formula - it changes the moment you type)
APPLY_HEADERS = ["Interest", "Status", "Applied on", "Next step", "Due", "Notes", "Contacts", "Company", "Role",
                 "Link", "Location", "Salary", "Experience ask", "Flags"]
OWNER_HEADERS = ("Decision", "Interest", "Status", "Notes", "Next step", "Due")
ID_COL = HEADERS.index("Posting ID")
PICK_OPTIONS = ["Pass", "1", "2", "3", "4", "5"]      # the Interest column
STATUS_OPTIONS = ["Not started", "Drafting", "Applied", "Networking", "Interviewing", "Offer",
                  "Rejected", "Withdrawn", "No response"]
ENDED = {"Rejected", "Withdrawn", "No response"}
STATUS_ORDER = {"Offer": 0, "Interviewing": 1, "Networking": 2, "Applied": 3, "Drafting": 4,
                "Not started": 5, "": 5, "Rejected": 7, "Withdrawn": 8, "No response": 9}
TARGET_STATUSES = {"Target", "Networking", "Coffee chat", "Referred", "Applied", "Interviewing", "Offer",
                   "Watching"}      # company statuses you set on the old Companies tab (kept in the database)
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
        return PICKS
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



def _pick_norm(v) -> str:
    """'3', '3.0' or 3 -> '3'; anything else as typed."""
    v = str(v or "").strip()
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else v
    except ValueError:
        return v


def read_owner_values(existing: dict[str, list[list[str]]],
                      known: dict[str, dict] | None = None,
                      resolve=None) -> tuple[dict[str, dict], dict[str, dict]]:
    """Your cells per posting, from To review and Apply. Each column is taken from wherever it
    differs from what was saved last run (that's the one you just edited)."""
    known = known or {}
    owner: dict[str, dict] = {}
    full: dict[str, dict] = {}

    def saved(pid: str) -> dict:
        k = known.get(pid) or {}
        return {"Interest": _pick_cell(k), "Status": k.get("status") or "", "Notes": k.get("notes") or "",
                "Next step": k.get("next_step") or "", "Due": k.get("due") or ""}
    review_score: dict[str, str] = {}
    for tab in (JOBS, PICKS):
        rows = existing.get(tab) or []
        if not rows:
            continue
        idx = {h: i for i, h in enumerate(rows[0])}
        if "Posting ID" not in idx:
            continue
        seen: set[str] = set()
        for raw in rows[1:]:
            raw = _pad(raw, len(rows[0]))
            cells = {h: (raw[i] or "") for h, i in idx.items()}
            pid = cells["Posting ID"]
            if resolve is not None:
                # sorting part of the Sheet can leave the hidden ID column behind: trust the
                # row's own Company + Role (+ Location), not the ID, and skip rows we can't place
                pid = resolve(pid, cells)
            if not pid:
                continue
            vals = {h: cells[h].strip() for h in OWNER_HEADERS if h in idx and h != "Decision"}
            if "Interest" in vals:
                vals["Interest"] = _pick_norm(vals["Interest"])
                if tab == JOBS:
                    review_score[pid] = vals["Interest"]
                elif not vals["Interest"] or vals["Interest"] == review_score.get(pid, vals["Interest"]):
                    del vals["Interest"]     # the old written Apply (before Nov 9's view): only real edits
            if pid in seen and not any(vals.values()):
                continue                       # a stale duplicate row never wipes real entries
            seen.add(pid)
            base = saved(pid)
            cur = owner.setdefault(pid, dict(base))
            for h, v in vals.items():
                if v != (base.get(h) or "").strip() or pid not in known:
                    cur[h] = v
            if tab == JOBS or pid not in full:
                full[pid] = cells
    return {pid: _from_pick(v) for pid, v in owner.items()}, full


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
    return _dedupe(rows)


_SEARCH_PREFIXES = {"muse", "jsearch", "adzuna", "jobspipe", "serpapi", "coresignal"}
_CO_SUFFIX = re.compile(r"\b(?:inc|llc|ltd|corp|corporation|co|company|labs?|technologies|technology|hq|ai|the)\b")


_PLACE_TAIL = re.compile(
    r"\b(?:remote|hybrid|on-?site|in[- ]office|new york|nyc|ny|manhattan|brooklyn|jersey city|hoboken|nj|"
    r"san francisco|sf|bay area|california|ca|los angeles|la|san diego|palo alto|mountain view|chicago|il|miami|"
    r"florida|fl|tampa|orlando|toronto|ontario|vancouver|canada|austin|dallas|houston|texas|tx|boston|seattle|"
    r"denver|atlanta|washington|dc|london|us|usa|u\.s\.|united states|north america|americas|emea|apac)\b")


def _same_job_key(c: dict) -> str:
    """Company + title, loosely: 'Clay Labs, Inc.' = 'Clay'; punctuation, seniority marks
    in brackets and the city are ignored, so one job found by two sources shows once."""
    co = re.sub(r"[^a-z0-9]", "", _CO_SUFFIX.sub(" ", (c.get("Company") or "").lower()))
    title = re.sub(r"^=HYPERLINK\(\"[^\"]*\",\s*\"(.*)\"\)$", r"\1", c.get("Role") or "")
    title = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title.lower())
    # 'Ops Associate - New York' / '| Remote' is the same job as 'Ops Associate'; a team after the
    # dash is not ('Chief of Staff - Finance' and 'Chief of Staff - Product' are two jobs)
    m = re.search(r"\s[-–—|,]\s(.*)$", title)
    if m:
        tail = m.group(1)
        city = re.split(r" \(tier| ·|,", (c.get("Location") or "").lower())[0].strip()
        if _PLACE_TAIL.search(tail) or (city and city in tail):
            title = title[:m.start()]
    return co + "|" + " ".join(re.findall(r"[a-z0-9]+", title))


def _company_words(c: dict) -> list[str]:
    return re.findall(r"[a-z0-9]+", _CO_SUFFIX.sub(" ", (c.get("Company") or "").lower()))


def _merge_company_variants(groups: dict[str, list[dict]]) -> None:
    """'Finch' (job site) and 'Finch Legal' (its own board) are one company: the same title in the
    same city under a company name that is the other's first word(s) is the same job (Nov 9)."""
    city = lambda r: re.split(r" \(tier| ·|,", (r["cells"].get("Location") or "").lower())[0].strip()
    by_title: dict[str, list[str]] = {}
    for k in groups:
        by_title.setdefault(k.split("|", 1)[1], []).append(k)
    for keys in by_title.values():
        if len(keys) < 2:
            continue
        keys.sort(key=lambda k: len(_company_words(groups[k][0]["cells"])))
        for i, short in enumerate(keys):
            if short not in groups:
                continue
            sw = _company_words(groups[short][0]["cells"])
            if not sw:
                continue
            for long in keys[i + 1:]:
                if long not in groups:
                    continue
                lw = _company_words(groups[long][0]["cells"])
                if lw[:len(sw)] == sw and len(lw) > len(sw) \
                        and {city(r) for r in groups[short]} & {city(r) for r in groups[long]}:
                    groups[long].extend(groups.pop(short))
                    break


def _dedupe(rows: list[dict]) -> list[dict]:
    """No duplicates on the Sheet (Nov 9). Rows you touched always stay; among untouched
    copies of one job, keep the open one, in your best city, from the company board, found earliest."""
    def touched(r):
        c = r["cells"]
        return bool(c["Interest"] or c["Status"] or c["Notes"] or c["Next step"]) or c["Decision"] != "Pending"
    def rank(r):
        return (not touched(r), r["closed"], r.get("tier", 9), (r["posting_id"] or "").split(":")[0] in _SEARCH_PREFIXES,
                r["first_seen"] or "")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(_same_job_key(r["cells"]), []).append(r)
    _merge_company_variants(groups)
    keep = set()
    for g in groups.values():
        g.sort(key=rank)
        keep.add(id(g[0]))
        city = lambda r: re.split(r" \(tier| ·", r["cells"].get("Location") or "")[0].strip()
        others = [x for x in dict.fromkeys(city(r) for r in g[1:]) if x and x != city(g[0])]
        if others:                    # one job posted in two cities shows once, best city first
            g[0]["cells"]["Location"] = f'{g[0]["cells"].get("Location") or ""} · also {", ".join(others)}'

        keep |= {id(r) for r in g[1:] if touched(r)}
    return [r for r in rows if id(r) in keep]


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
        "Source": d.get("source", ""), "Link": r["url"] or "", "Posting ID": r["posting_id"],
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
    # Nov 9: mostly AI startups - an AI company (in your fields or not) before a non-AI
    # hospitality / real-estate company
    for rank, mark in enumerate(("Your edge", "AI-forward company", "Preferred field"), start=1):
        if mark in flags:
            return rank
    return 4


def _sort_key(tab: str, row: dict):
    c = row["cells"]
    if tab == PICKS:
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
    resolve = make_row_resolver(store)
    sheet_owner, full_rows = read_owner_values(existing, store.owner_states(), resolve)
    for pid, vals in sheet_owner.items():          # the Sheet is the source of truth for your columns
        store.save_owner_state(pid, vals, ts)
    store.commit()
    states = store.owner_states()
    rows = build_rows(store, criteria, ts, states)
    db_ids = {r["posting_id"] for r in rows}
    for pid, vals in sheet_owner.items():          # touched rows that left the DB view are kept
        if pid not in db_ids and any(v for k, v in vals.items() if k != "Decision" or v != "Pending"):
            cells = {h: full_rows[pid].get(h, "") for h in HEADERS}
            cells.update({"Interest": _pick_cell(states.get(pid) or {}), "Status": vals.get("Status", ""),
                          "Notes": vals.get("Notes", ""), "Next step": vals.get("Next step", ""),
                          "Due": vals.get("Due", "")})
            known = store.conn.execute("SELECT title, url FROM postings WHERE posting_id=?", (pid,)).fetchone()
            if known and known["url"]:
                cells["Role"] = hyperlink(known["url"], known["title"] or cells.get("Role", ""))
                cells["Link"] = known["url"]
            rows.append({"posting_id": pid, "closed": True, "first_seen": "", "cells": cells})

    # To review keeps YOUR row order (however you sorted); new finds go on top, best first
    order: list[str] = []
    jr = existing.get(JOBS) or []
    if jr:
        idx = {h: i for i, h in enumerate(jr[0])}
        if "Posting ID" in idx:
            for raw in jr[1:]:
                raw = _pad(raw, len(jr[0]))
                pid = resolve(raw[idx["Posting ID"]], {h: (raw[i] or "") for h, i in idx.items()})
                if pid and pid not in order:
                    order.append(pid)
    pos = {pid: i for i, pid in enumerate(order)}
    new = sorted([r for r in rows if r["posting_id"] not in pos], key=lambda r: _sort_key("new finds", r))
    kept = sorted([r for r in rows if r["posting_id"] in pos], key=lambda r: pos[r["posting_id"]])
    scored = lambda r: bool((r["cells"].get("Interest") or "").strip())
    tab_rows = [r for r in new + kept if not scored(r)] + [r for r in new + kept if scored(r)]
    grid = [HEADERS] + [[r["cells"].get(h, "") for h in HEADERS] for r in tab_rows]
    requests = _grid_tab(meta[JOBS], HEADERS, grid, len(existing.get(JOBS) or []))
    picks = [r for r in tab_rows if _interest(r["cells"].get("Interest") or "") > 0]
    passed = sum(1 for r in tab_rows if (r["cells"].get("Interest") or "") == "Pass")
    return requests, {"rows": len(rows), "tabs": {JOBS: len(tab_rows), PICKS: len(picks), PASSED: passed},
                      "new": len(new)}


def _apply_view(meta: dict) -> list[dict]:
    """Apply: To review's 1-5s, best first, in Apply's column order - one live formula."""
    sid = meta[PICKS]["id"]
    last = _col_letter(len(HEADERS) - 1)
    c = lambda h: f"CHOOSECOLS(g, {HEADERS.index(h) + 1})"
    cols = []
    for h in APPLY_HEADERS:
        if h == "Contacts":
            cols.append(f'ARRAYFORMULA(IF(COUNTIF(Contacts!A:A, {c("Company")})=0, "", '
                        f'COUNTIF(Contacts!A:A, {c("Company")})))')
        else:
            cols.append(c(h))
    score = f"IFERROR(VALUE('{JOBS}'!A2:A), 0)"
    formula = (f"=IFERROR(LET("
               f"f, FILTER('{JOBS}'!A2:{last}, {score}>=1), "
               f"g, SORT(f, ARRAYFORMULA(REGEXMATCH(CHOOSECOLS(f, {HEADERS.index('Status') + 1}), "
               f"\"^({'|'.join(sorted(ENDED))})$\")), TRUE, ARRAYFORMULA(VALUE(CHOOSECOLS(f, 1))), FALSE), "
               f"HSTACK({', '.join(cols)})), \"\")")      # finished applications last
    reqs = _clear_tab(sid) + [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": 2000, "columnCount": max(meta[PICKS].get("cols", 26), len(APPLY_HEADERS)),
            "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(h) for h in APPLY_HEADERS]}, {"values": [_cell(formula)]}],
                         "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        _interest_format(sid),
    ]
    col = {h: _col_letter(i) for i, h in enumerate(APPLY_HEADERS)}
    full = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(APPLY_HEADERS)}
    rules = [(f'=OR(' + ",".join(f'${col["Status"]}2="{x}"' for x in sorted(ENDED)) + ')',
              {"textFormat": {"foregroundColor": GRAY}}),
             (f'=REGEXMATCH(${col["Flags"]}2,"Closed")', {"textFormat": {"foregroundColor": GRAY}}),
             (f'=REGEXMATCH(${col["Flags"]}2,"Follow up")', {"backgroundColor": YELLOW}),
             (_due_formula(col["Due"]), {"backgroundColor": YELLOW}),
             (f'=VALUE(0&$A2)=5', {"backgroundColor": GREEN_5}), (f'=VALUE(0&$A2)=4', {"backgroundColor": GREEN_4}),
             (f'=VALUE(0&$A2)=3', {"backgroundColor": GREEN_3}),
             (f'=IFERROR(VALUE(0&$A2)>=1,FALSE)', {"backgroundColor": GREEN_1})]
    if meta[PICKS].get("cf_formulas") != [f for f, _ in rules]:
        reqs += [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}}
                 for _ in range(int(meta[PICKS].get("cf_count", 0)))]
        for i, (f, fmt) in enumerate(rules):
            reqs.append({"addConditionalFormatRule": {"index": i, "rule": {"ranges": [full], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f}]}, "format": fmt}}}})
    return reqs


def _interest_format(sid: int) -> dict:
    """Interest as plain text, left-aligned: a picked '4' and a typed 4 look the same."""
    return {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
                           "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"},
                                                          "horizontalAlignment": "LEFT"}},
                           "fields": "userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment"}}


def _clear_tab(sid: int) -> list[dict]:
    """Empty a tab (values, dropdowns, filter) so a formula can fill it."""
    whole = {"sheetId": sid}
    return [{"updateCells": {"range": whole, "fields": "userEnteredValue"}},
            {"setDataValidation": {"range": whole}},
            {"clearBasicFilter": {"sheetId": sid}}]


def _view_tab(meta: dict, tab: str, condition: str) -> list[dict]:
    """Passed: one formula that shows matching To review rows live."""
    sid = meta[tab]["id"]
    last = _col_letter(len(HEADERS) - 1)
    formula = f"={{'{JOBS}'!A1:{last}1; IFERROR(FILTER('{JOBS}'!A2:{last}, {condition}), \"\")}}"
    return [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": 2000, "columnCount": max(meta[tab].get("cols", 26), len(HEADERS)), "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(formula)]}], "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                                                 "startIndex": ID_COL, "endIndex": ID_COL + 1},
                                       "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
    ]


def _grid_tab(m: dict, headers: list[str], grid: list[list], existing_len: int, room: int = 20) -> list[dict]:
    """To review / Apply: values, dropdowns, filter, hidden ID, colours."""
    sid = m["id"]
    # the grid is resized to the rows plus some room (shrinking deletes the rest); old rows
    # inside that room are blanked
    need_rows = len(grid) + room
    padded = grid + [[""] * len(headers)] * (min(existing_len, need_rows) - len(grid))
    col = {h: i for i, h in enumerate(headers)}
    id_col = col["Posting ID"]
    reqs: list[dict] = [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": need_rows, "columnCount": max(m.get("cols", 26), len(headers)), "frozenRowCount": 1}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(v) for v in row]} for row in padded],
                         "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"setDataValidation": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": need_rows,
                                         "startColumnIndex": 0, "endColumnIndex": len(headers)}}},
        _validation(sid, col["Interest"], PICK_OPTIONS, need_rows),
        _interest_format(sid),
    ]
    if "Status" in col:
        reqs.append(_validation(sid, col["Status"], STATUS_OPTIONS, need_rows))
    reqs += [
        {"setBasicFilter": {"filter": {"range": {"sheetId": sid, "startRowIndex": 0,
                                                 "endRowIndex": len(grid),
                                                 "startColumnIndex": 0, "endColumnIndex": len(headers)}}}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                                                 "startIndex": id_col, "endIndex": id_col + 1},
                                       "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
    ]
    full = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(headers)}
    flags, interest = _col_letter(col["Flags"]), _col_letter(col["Interest"])
    # first matching rule wins: passed / closed / finished grey, follow-up / due yellow, then greens
    rules = [(f'=${interest}2="Pass"', {"textFormat": {"foregroundColor": GRAY}}),
             (f'=REGEXMATCH(${flags}2,"Closed")', {"textFormat": {"foregroundColor": GRAY}})]
    if "Status" in col:
        status = _col_letter(col["Status"])
        rules.append((f'=OR(' + ",".join(f'${status}2="{s}"' for s in sorted(ENDED)) + ')',
                      {"textFormat": {"foregroundColor": GRAY}}))
        rules.append((f'=REGEXMATCH(${flags}2,"Follow up")', {"backgroundColor": YELLOW}))
    if "Due" in col:
        rules.append((_due_formula(_col_letter(col["Due"])), {"backgroundColor": YELLOW}))
    rules += [(f'=VALUE(0&${interest}2)=5', {"backgroundColor": GREEN_5}),
              (f'=VALUE(0&${interest}2)=4', {"backgroundColor": GREEN_4}),
              (f'=VALUE(0&${interest}2)=3', {"backgroundColor": GREEN_3}),
              (f'=IFERROR(VALUE(0&${interest}2)>=1,FALSE)', {"backgroundColor": GREEN_1})]
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
CONTACTS = CONTACTS_TAB = "Contacts"
BACK_OFFICE = [DROPPED, UNPARSED, FUNNEL, HEALTH]      # hidden once; unhide any time
ALL_TABS = [JOBS, PICKS, PASSED, CONTACTS, DROPPED, UNPARSED, FUNNEL, HEALTH]
REMOVED_TABS = ["Home", "Companies", "Grad programs", "Company jobs", "Before reset"]   # Nov 9: not needed

from ..pipeline.programs import PROGRAM_RX  # noqa: E402  (new grad / rotational program titles)


def is_program(title: str) -> bool:
    return bool(PROGRAM_RX.search(title or ""))


# --- Contacts tab (Nov 11 layout, the same for every company) ------------------------------------
# A-K arrive from a contact-sheet CSV you paste (16 columns, no header); L-P are yours to update.
# The pipeline sets the header, dropdowns, colours, filter views and two small fills (below); it
# never deletes or rewrites your rows.
CONTACT_HEADERS = ["Company", "Job", "Name", "Role / team", "Location", "LinkedIn", "How you're connected",
                   "Priority", "Why", "Angle", "Message", "Status", "Last contact", "Next step", "Due", "Notes"]
CONTACT_PRIORITY = ["1", "2", "Hold"]
CONTACT_STATUS = ["Not sent", "Sent", "Accepted", "Replied", "Meeting set", "Met", "No reply"]
CONTACT_NEXT = ["Send note", "Follow up", "Book coffee", "Send thanks", "None"]
CONTACT_FOLLOW_UP_DAYS = 7          # Due = Last contact + 7 once Status is Sent (only when Due is empty)
CONTACT_MESSAGE_MAX = 300
RED_FILL = {"red": 0.96, "green": 0.8, "blue": 0.8}
CONTACT_VIEWS = ["Send today", "Follow-ups due", "By company"]


def _range_validation(sid: int, col: int, source: str, rows: int) -> dict:
    return {"setDataValidation": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": rows, "startColumnIndex": col,
                  "endColumnIndex": col + 1},
        "rule": {"condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": source}]},
                 "showCustomUi": True, "strict": False}}}


def _ccol(h: str) -> int:
    return CONTACT_HEADERS.index(h)


def _contacts_tab(meta: dict) -> list[dict]:
    """Layout only: header, frozen panes, widths, one-line rows, dropdowns, date formats, colours and
    the default sort (Company, Priority, Name)."""
    m = meta[CONTACTS]
    sid = m["id"]
    rows = max(m.get("rows", 1000), 500)
    n = len(CONTACT_HEADERS)
    L = {h: _col_letter(_ccol(h)) for h in CONTACT_HEADERS}

    def cols(a: str, b: str | None = None) -> dict:
        return {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": _ccol(a), "endColumnIndex": _ccol(b or a) + 1}

    def width(h: str, px: int) -> dict:
        return {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                                                        "startIndex": _ccol(h), "endIndex": _ccol(h) + 1},
                                              "properties": {"pixelSize": px}, "fields": "pixelSize"}}
    reqs = [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "rowCount": rows, "columnCount": max(m.get("cols", 26), n), "frozenRowCount": 1, "frozenColumnCount": 3}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount,"
                      "gridProperties.frozenColumnCount"}},
        {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 0},
                         "rows": [{"values": [_cell(h) for h in CONTACT_HEADERS]}], "fields": "userEnteredValue"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        # every row one line tall: long text is clipped, never wrapped
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": n},
                        "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP", "verticalAlignment": "MIDDLE"}},
                        "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment"}},
        {"repeatCell": {"range": cols("Last contact"), "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}}, "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": cols("Due"), "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}}, "fields": "userEnteredFormat.numberFormat"}},
        width("Company", 110), width("Job", 200), width("Name", 170), width("Role / team", 240),
        width("Location", 110), width("LinkedIn", 160), width("How you're connected", 200), width("Priority", 75),
        width("Why", 220), width("Angle", 220), width("Message", 260), width("Status", 105),
        width("Last contact", 100), width("Next step", 110), width("Due", 95), width("Notes", 220),
        # clears dropdowns left over from earlier layouts before setting this one's
        {"setDataValidation": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": rows,
                                         "startColumnIndex": 0, "endColumnIndex": max(n, 16)}}},
        _validation(sid, _ccol("Priority"), CONTACT_PRIORITY, rows),
        _validation(sid, _ccol("Status"), CONTACT_STATUS, rows),
        _validation(sid, _ccol("Next step"), CONTACT_NEXT, rows),
        # default sort: Company, then Priority (1, 2, Hold), then Name
        {"setBasicFilter": {"filter": {"range": {"sheetId": sid, "startRowIndex": 0, "startColumnIndex": 0,
                                                 "endColumnIndex": n},
                                       "sortSpecs": [{"dimensionIndex": _ccol("Company"), "sortOrder": "ASCENDING"},
                                                     {"dimensionIndex": _ccol("Priority"), "sortOrder": "ASCENDING"},
                                                     {"dimensionIndex": _ccol("Name"), "sortOrder": "ASCENDING"}]}}},
    ]
    msg, due, st, pr = L["Message"], L["Due"], L["Status"], L["Priority"]
    rules = [
        (f'=LEN(${msg}2)>{CONTACT_MESSAGE_MAX}', {"backgroundColor": RED_FILL}, cols("Message")),
        (f'=AND(ISNUMBER(${due}2),${due}2<=TODAY(),OR(${st}2="Sent",${st}2="Accepted"))',
         {"backgroundColor": RED_FILL}, cols("Due")),
        (f'=TO_TEXT(${pr}2)="Hold"', {"textFormat": {"foregroundColor": GRAY}},
         {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": n}),
    ]
    if m.get("cf_formulas") != [f for f, _, _ in rules]:
        for _ in range(int(m.get("cf_count", 0))):
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}})
        for i, (formula, fmt, rng) in enumerate(rules):
            reqs.append({"addConditionalFormatRule": {"index": i, "rule": {"ranges": [rng], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                "format": fmt}}}})
    return reqs


def _contact_filter_views(meta: dict) -> list[dict]:
    """Saved filter views, created once (yours to edit afterwards)."""
    m = meta[CONTACTS]
    have = set(m.get("filter_views") or [])
    sid, n = m["id"], len(CONTACT_HEADERS)
    L = {h: _col_letter(_ccol(h)) for h in CONTACT_HEADERS}
    rng = {"sheetId": sid, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": n}
    sort = [{"dimensionIndex": _ccol("Company"), "sortOrder": "ASCENDING"},
            {"dimensionIndex": _ccol("Priority"), "sortOrder": "ASCENDING"},
            {"dimensionIndex": _ccol("Name"), "sortOrder": "ASCENDING"}]

    def formula(col: str, f: str) -> dict:
        return {"columnIndex": _ccol(col), "filterCriteria": {"condition": {
            "type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f}]}}}
    views = {
        "Send today": [formula("Priority", f'=TO_TEXT({L["Priority"]}2)="1"'),
                       formula("Status", f'={L["Status"]}2="Not sent"')],
        "Follow-ups due": [formula("Due", f'=AND(ISNUMBER({L["Due"]}2),{L["Due"]}2<=TODAY(),'
                                         f'OR({L["Status"]}2="Sent",{L["Status"]}2="Accepted"))')],
        "By company": [],
    }
    return [{"addFilterView": {"filter": {"title": t, "range": rng, "sortSpecs": sort, "filterSpecs": specs}}}
            for t, specs in views.items() if t not in have]


def _parse_day(v: str):
    from datetime import date
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def contact_fills(existing: list[list[str]]) -> list[tuple[int, int, dict]]:
    """(row, col, cell) writes on your rows - only into empty cells:
    - Due = Last contact + 7 when Status is Sent and Due is empty (you can overwrite it);
    - a new row's Status = Not sent, and Next step = Send note (Priority 1 / 2) or None (Hold)."""
    from datetime import timedelta
    if not existing or [str(x).strip() for x in existing[0][:len(CONTACT_HEADERS)]] != CONTACT_HEADERS:
        return []
    out = []
    for i, r in enumerate(existing[1:], start=1):
        r = _pad(list(r), len(CONTACT_HEADERS))
        if not str(r[_ccol("Name")]).strip():
            continue
        status, pr = str(r[_ccol("Status")]).strip(), str(r[_ccol("Priority")]).strip()
        if not status:
            out.append((i, _ccol("Status"), _cell("Not sent")))
        if not str(r[_ccol("Next step")]).strip() and pr and (not status or status == "Not sent"):
            out.append((i, _ccol("Next step"), _cell("None" if pr.lower() == "hold" else "Send note")))
        if status == "Sent" and not str(r[_ccol("Due")]).strip():
            last = _parse_day(str(r[_ccol("Last contact")]))
            if last:
                d = last + timedelta(days=CONTACT_FOLLOW_UP_DAYS)
                out.append((i, _ccol("Due"), {"userEnteredValue": {"formulaValue": f"=DATE({d.year},{d.month},{d.day})"}}))
    return out


def _contact_fill_requests(sid: int, fills: list[tuple[int, int, dict]]) -> list[dict]:
    return [{"updateCells": {"start": {"sheetId": sid, "rowIndex": r, "columnIndex": c},
                             "rows": [{"values": [cell]}], "fields": "userEnteredValue"}} for r, c, cell in fills]


CONTACT_IMPORT_PREFIX = "contacts -"     # a spreadsheet titled "Contacts - <Company>" shared with the pipeline


def contact_import_rows(imports: list[tuple[str, list[list[str]]]], existing: list[list[str]]) -> list[list[str]]:
    """Rows from shared contact sheets ("Contacts - EliseAI": the same 16 columns, header row optional)
    for people not already on Contacts (same company + name)."""
    def key(company, name):
        return (re.sub(r"\W+", "", (company or "").lower()), re.sub(r"\W+", "", (name or "").lower()))
    ci, ni = _ccol("Company"), _ccol("Name")
    have = {key(_pad(list(r), 16)[ci], _pad(list(r), 16)[ni]) for r in (existing or [])[1:] if any(r)}
    out = []
    for _title, grid in imports:
        rows = grid[1:] if grid and str(_pad(list(grid[0]), 16)[ni]).strip().lower() == "name" else grid
        for r in rows:
            r = [str(x).strip() for x in _pad(list(r), len(CONTACT_HEADERS))][:len(CONTACT_HEADERS)]
            k = key(r[ci], r[ni])
            if not k[1] or k in have:
                continue
            have.add(k)
            out.append(r)
    return out


def _contact_append_requests(sid: int, start_row: int, rows: list[list[str]]) -> list[dict]:
    if not rows:
        return []
    return [{"updateCells": {"start": {"sheetId": sid, "rowIndex": start_row, "columnIndex": 0},
                             "rows": [{"values": [_cell(v) for v in r]} for r in rows], "fields": "userEnteredValue"}}]


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
    if hasattr(backend, "rename_tabs"):
        backend.rename_tabs(RENAMES)
    if hasattr(backend, "delete_tabs") and not store.conn.execute(
            "SELECT 1 FROM sheet_tabs WHERE tab='__four_tabs_v1'").fetchone():
        backend.delete_tabs(REMOVED_TABS)                      # once: you can re-add a tab any time
        store.conn.execute("DELETE FROM sheet_tabs WHERE tab IN (?, ?)", (PICKS, CONTACTS))   # rebuilt
        store.conn.execute("INSERT OR REPLACE INTO sheet_tabs (tab, content_hash, written_at) "
                           "VALUES ('__four_tabs_v1', '', ?)", (ts,))
    meta = backend.ensure_tabs(ALL_TABS)
    existing = backend.read(ALL_TABS)
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
    per_tab[CONTACTS] = []
    contacts = existing.get(CONTACTS) or []
    if contacts and "Covers" in [str(x).strip() for x in contacts[0]]:
        # Nov 11: rows from the short-lived 14-column layout are cleared once (you re-paste them in 16)
        per_tab[CONTACTS].append({"updateCells": {"range": {"sheetId": meta[CONTACTS]["id"], "startRowIndex": 0},
                                                  "fields": "userEnteredValue"}})
        contacts = []
        log.info("Contacts: cleared the rows from the old layout")
    # fills and additions go in before the layout, whose default sort then reorders the rows
    per_tab[CONTACTS] += _contact_fill_requests(meta[CONTACTS]["id"], contact_fills(contacts))
    imports = backend.contact_imports() if hasattr(backend, "contact_imports") else []
    new_contacts = contact_import_rows(imports, contacts)
    if new_contacts:
        per_tab[CONTACTS] += _contact_append_requests(meta[CONTACTS]["id"], max(1, len(contacts)), new_contacts)
        log.info("Contacts: %d added from %s", len(new_contacts), ", ".join(t for t, _ in imports))
    per_tab[CONTACTS] += _contacts_tab(meta)
    views = _contact_filter_views(meta)
    per_tab[PICKS] = _apply_view(meta)
    per_tab[PASSED] = _view_tab(meta, PASSED, f"'{JOBS}'!A2:A=\"Pass\"")
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
    for tab in [t for t in ALL_TABS if meta[t].get("index") != ALL_TABS.index(t)]:   # keep the tab order
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
    requests += views                            # saved filter views, created once
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
        self.gc = gc
        self.ss = gc.open_by_key(sheet_id)

    def ensure_tabs(self, tabs: list[str]) -> dict[str, dict]:
        meta = self.ss.fetch_sheet_metadata({"fields": "sheets(properties,conditionalFormats,filterViews(title))"})
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
                      "filter_views": [fv.get("title") for fv in have[t].get("filterViews") or []],
                      "cf_formulas": [((((cf.get("booleanRule") or {}).get("condition") or {}).get("values")
                                        or [{}])[0]).get("userEnteredValue")
                                      for cf in have[t].get("conditionalFormats") or []]}
        return out

    def rename_tabs(self, renames: dict[str, str]) -> None:
        meta = self.ss.fetch_sheet_metadata({"fields": "sheets(properties)"})
        have = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
        reqs = [{"updateSheetProperties": {"properties": {"sheetId": have[old], "title": new}, "fields": "title"}}
                for old, new in renames.items() if old in have and new not in have]
        if reqs:
            self.ss.batch_update({"requests": reqs})

    def delete_tabs(self, names: list[str]) -> None:
        meta = self.ss.fetch_sheet_metadata({"fields": "sheets(properties)"})
        reqs = [{"deleteSheet": {"sheetId": s["properties"]["sheetId"]}} for s in meta["sheets"]
                if s["properties"]["title"] in names]
        if reqs and len(reqs) < len(meta["sheets"]):
            self.ss.batch_update({"requests": reqs})

    def contact_imports(self) -> list[tuple[str, list[list[str]]]]:
        """Spreadsheets shared with the pipeline whose title starts "Contacts - " (first tab)."""
        out = []
        try:
            for sp in self.gc.openall():
                if sp.title.strip().lower().startswith(CONTACT_IMPORT_PREFIX) and sp.id != self.ss.id:
                    out.append((sp.title, sp.sheet1.get_all_values()))
        except Exception as exc:          # never stops the Sheet write
            log.warning("contact sheets: %s", exc)
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

    def rename_tabs(self, renames: dict[str, str]) -> None:
        for old, new in renames.items():
            if old in self.tabs and new not in self.tabs:
                self.tabs[new] = self.tabs.pop(old)
        self._save()

    def delete_tabs(self, names: list[str]) -> None:
        for n in names:
            self.tabs.pop(n, None)
        self._save()

    def _tab_by_id(self, sid: int) -> dict:
        return next(t for t in self.tabs.values() if t["id"] == sid)

    def batch_update(self, requests: list[dict]) -> None:
        self.calls.append(requests)
        for req in requests:
            (kind, body), = req.items()
            if kind == "updateCells" and "range" in body:          # clear a whole tab
                tab = self._tab_by_id(body["range"]["sheetId"])
                tab["grid"] = []
            elif kind == "updateCells":
                tab = self._tab_by_id(body["start"]["sheetId"])
                r0 = body["start"]["rowIndex"]
                for i, row in enumerate(body["rows"]):
                    while len(tab["grid"]) <= r0 + i:
                        tab["grid"].append([])
                    new = [c.get("userEnteredValue", {}) for c in row["values"]]
                    c0 = body["start"].get("columnIndex", 0)
                    old = tab["grid"][r0 + i]           # like Sheets: only the cells sent change
                    old = old + [{}] * max(0, c0 - len(old))
                    tab["grid"][r0 + i] = old[:c0] + new + old[c0 + len(new):]
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
            if f.startswith("=IFERROR(INDEX("):            # Apply's live score: Sheets shows the score
                return f.rsplit(', "', 1)[1].rstrip('")')
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

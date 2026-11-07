"""SQLite store — the source of truth; the Google Sheet is a view of it.

Tables: companies, postings, scores, runs, board_health.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import ROOT

DEFAULT_DB = ROOT / "data" / "pipeline.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    name TEXT PRIMARY KEY, platform TEXT, slug TEXT
);
CREATE TABLE IF NOT EXISTS postings (
    posting_id TEXT PRIMARY KEY,
    company TEXT, title TEXT, url TEXT, posted_at TEXT,
    first_seen TEXT, last_seen TEXT, closed_at TEXT,
    is_new INTEGER DEFAULT 1, evergreen INTEGER DEFAULT 0,
    snapshot TEXT            -- full raw posting, stored the first time it is seen
);
CREATE TABLE IF NOT EXISTS scores (       -- rule results (no scoring since v2)
    posting_id TEXT PRIMARY KEY REFERENCES postings(posting_id),
    passed INTEGER, gate TEXT, matched TEXT, failures TEXT, dropped_at TEXT,
    flags TEXT,
    display TEXT,            -- JSON of Sheet-ready facts (field, visa, salary, ...)
    evaluated_at TEXT
);
CREATE TABLE IF NOT EXISTS owner_state (   -- your Sheet columns, mirrored
    posting_id TEXT PRIMARY KEY,
    decision TEXT, interest TEXT, status TEXT, notes TEXT,
    applied_on TEXT,          -- first time Status was seen as Applied
    status_changed_at TEXT,   -- last time Status changed (follow-up clock)
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
    boards_polled INTEGER, postings_seen INTEGER, new INTEGER, passed INTEGER, errors INTEGER
);
CREATE TABLE IF NOT EXISTS board_health (
    company TEXT PRIMARY KEY, platform TEXT, last_success TEXT, last_error TEXT,
    last_error_at TEXT, count_last INTEGER, count_prev INTEGER, status TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # --- companies / postings ------------------------------------------
    def upsert_company(self, name: str, platform: str, slug: str) -> None:
        self.conn.execute(
            "INSERT INTO companies(name, platform, slug) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET platform=excluded.platform, slug=excluded.slug",
            (name, platform, slug))

    def upsert_posting(self, posting_id: str, company: str, title: str, url: str,
                       posted_at: str | None, snapshot: Any, ts: str) -> dict:
        """Insert or refresh a posting. Returns {first_seen, is_new}."""
        row = self.conn.execute("SELECT first_seen FROM postings WHERE posting_id=?", (posting_id,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO postings(posting_id, company, title, url, posted_at, first_seen, last_seen, "
                "closed_at, is_new, snapshot) VALUES (?,?,?,?,?,?,?,NULL,1,?)",
                (posting_id, company, title, url, posted_at, ts, ts, json.dumps(snapshot, default=str)))
            return {"first_seen": ts, "is_new": True}
        self.conn.execute(
            "UPDATE postings SET title=?, url=?, posted_at=COALESCE(?, posted_at), last_seen=?, "
            "closed_at=NULL, is_new=0 WHERE posting_id=?", (title, url, posted_at, ts, posting_id))
        return {"first_seen": row["first_seen"], "is_new": False}

    def close_missing(self, company: str, seen_ids: Iterable[str], ts: str) -> int:
        seen = set(seen_ids)
        rows = self.conn.execute(
            "SELECT posting_id FROM postings WHERE company=? AND closed_at IS NULL", (company,)).fetchall()
        missing = [r["posting_id"] for r in rows if r["posting_id"] not in seen]
        self.conn.executemany("UPDATE postings SET closed_at=?, is_new=0 WHERE posting_id=?",
                              [(ts, pid) for pid in missing])
        return len(missing)

    def mark_evergreen(self, days: int, ts: str) -> None:
        cutoff = (datetime.fromisoformat(ts) - timedelta(days=days)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE postings SET evergreen = CASE WHEN closed_at IS NULL AND first_seen <= ? "
                          "THEN 1 ELSE 0 END", (cutoff,))

    # --- scores ---------------------------------------------------------
    def save_score(self, posting_id: str, passed: bool, failures: list[dict], flags: list[str],
                   display: dict, ts: str) -> None:
        prev = self.conn.execute("SELECT passed, dropped_at FROM scores WHERE posting_id=?",
                                 (posting_id,)).fetchone()
        dropped_at = None
        if not passed:
            dropped_at = prev["dropped_at"] if prev is not None and prev["passed"] == 0 and prev["dropped_at"] else ts
        first = failures[0] if failures else {}
        self.conn.execute(
            "INSERT OR REPLACE INTO scores(posting_id, passed, gate, matched, failures, dropped_at, flags, "
            "display, evaluated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (posting_id, int(passed), first.get("gate"), first.get("matched"), json.dumps(failures),
             dropped_at, json.dumps(flags), json.dumps(display, default=str), ts))

    def pipeline_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.*, s.* FROM postings p JOIN scores s USING(posting_id) WHERE s.passed=1").fetchall()

    def dropped_rows(self, days: int, ts: str) -> list[sqlite3.Row]:
        cutoff = (datetime.fromisoformat(ts) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.conn.execute(
            "SELECT p.company, p.title, p.url, s.gate, s.matched, s.dropped_at FROM postings p "
            "JOIN scores s USING(posting_id) WHERE s.passed=0 AND s.dropped_at >= ? "
            "ORDER BY s.dropped_at DESC", (cutoff,)).fetchall()

    # --- your columns -----------------------------------------------------
    def save_owner_state(self, posting_id: str, vals: dict, ts: str) -> None:
        """Mirror Decision / Interest / Status / Notes; stamp Applied on and status changes."""
        prev = self.conn.execute("SELECT * FROM owner_state WHERE posting_id=?", (posting_id,)).fetchone()
        status = vals.get("Status", "") or ""
        applied_on = prev["applied_on"] if prev is not None else None
        changed_at = prev["status_changed_at"] if prev is not None else None
        if prev is None or (prev["status"] or "") != status:
            changed_at = ts
        if status == "Applied" and not applied_on:
            applied_on = ts
        self.conn.execute(
            "INSERT OR REPLACE INTO owner_state(posting_id, decision, interest, status, notes, applied_on, "
            "status_changed_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (posting_id, vals.get("Decision", ""), vals.get("Interest", ""), status, vals.get("Notes", ""),
             applied_on, changed_at, ts))

    def owner_states(self) -> dict[str, dict]:
        return {r["posting_id"]: dict(r) for r in self.conn.execute("SELECT * FROM owner_state")}

    # --- runs / health --------------------------------------------------
    def record_run(self, **counts: Any) -> None:
        cols = ", ".join(counts)
        self.conn.execute(f"INSERT INTO runs({cols}) VALUES ({','.join('?' * len(counts))})",
                          tuple(counts.values()))

    def runs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY id").fetchall()

    def board_ok(self, company: str, platform: str, count: int, ts: str) -> str:
        prev = self.conn.execute("SELECT count_last FROM board_health WHERE company=?", (company,)).fetchone()
        prev_count = prev["count_last"] if prev is not None else None
        status = "Went to zero" if count == 0 and prev_count else "OK"
        self.conn.execute(
            "INSERT INTO board_health(company, platform, last_success, count_last, count_prev, status) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(company) DO UPDATE SET platform=excluded.platform, "
            "last_success=excluded.last_success, count_prev=board_health.count_last, "
            "count_last=excluded.count_last, status=excluded.status",
            (company, platform, ts, count, prev_count, status))
        return status

    def board_error(self, company: str, platform: str, error: str, ts: str) -> None:
        self.conn.execute(
            "INSERT INTO board_health(company, platform, last_error, last_error_at, status) VALUES (?,?,?,?,'Error') "
            "ON CONFLICT(company) DO UPDATE SET last_error=excluded.last_error, "
            "last_error_at=excluded.last_error_at, status='Error'", (company, platform, error[:500], ts))

    def board_health(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM board_health ORDER BY company").fetchall()

    def commit(self) -> None:
        self.conn.commit()

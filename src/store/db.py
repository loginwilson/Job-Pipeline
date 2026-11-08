"""SQLite store — the source of truth; the Google Sheet is a view of it.

Tables: companies, postings, scores, runs, board_health.
"""
from __future__ import annotations

import json
import os
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
    snapshot TEXT,           -- full raw posting, stored the first time it is seen
    dedupe_key TEXT,         -- company + title + city, to merge the same job from two sources
    merged_into TEXT         -- set when a search copy is replaced by the company-board copy
);
CREATE TABLE IF NOT EXISTS discovered_boards (   -- company boards found automatically
    company TEXT PRIMARY KEY, platform TEXT, slug TEXT, found_at TEXT, via TEXT, matched_title TEXT
);
CREATE TABLE IF NOT EXISTS probes (               -- companies checked for a board
    company TEXT PRIMARY KEY, probed_at TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS detail_cache (         -- per-posting details for boards that need them
    posting_id TEXT PRIMARY KEY, payload TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS search_pairs (         -- keyword x city searches, rotated
    source TEXT, pair TEXT, last_searched TEXT, last_count INTEGER, PRIMARY KEY (source, pair)
);
CREATE TABLE IF NOT EXISTS search_usage (         -- requests spent per day (budget)
    source TEXT, day TEXT, used INTEGER, PRIMARY KEY (source, day)
);
CREATE TABLE IF NOT EXISTS ats_links (            -- career-site links seen in search results (e.g. Workday)
    url_root TEXT PRIMARY KEY, platform TEXT, company TEXT, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS link_domains (         -- where search results send you to apply (coverage gaps)
    domain TEXT PRIMARY KEY, seen INTEGER
);
CREATE TABLE IF NOT EXISTS source_runs (          -- last full sweep per search source
    source TEXT PRIMARY KEY, last_complete TEXT
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
    boards_polled INTEGER, postings_seen INTEGER, new INTEGER, passed INTEGER, errors INTEGER,
    new_passed INTEGER,       -- new options added this run (new AND passed the must-haves)
    seconds REAL, coverage TEXT
);
CREATE TABLE IF NOT EXISTS run_sources (           -- per-source funnel for each run
    started_at TEXT, source TEXT, seen INTEGER, new INTEGER, passed INTEGER, new_passed INTEGER,
    dropped INTEGER, reasons TEXT, PRIMARY KEY (started_at, source)
);
CREATE TABLE IF NOT EXISTS board_health (
    company TEXT PRIMARY KEY, platform TEXT, last_success TEXT, last_error TEXT,
    last_error_at TEXT, count_last INTEGER, count_prev INTEGER, status TEXT,
    first_polled TEXT, last_pass_at TEXT
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
        for table, cols_needed in (("postings", {"dedupe_key": "TEXT", "merged_into": "TEXT"}),
                                   ("board_health", {"first_polled": "TEXT", "last_pass_at": "TEXT"}),
                                   ("runs", {"new_passed": "INTEGER", "seconds": "REAL", "coverage": "TEXT"})):
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols_needed.items():
                if col not in have:      # databases created before these columns existed
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # early runs stored Workday's per-source stats as 'workday'
        self.conn.execute("DELETE FROM run_sources WHERE source='workday' AND EXISTS "
                          "(SELECT 1 FROM run_sources r2 WHERE r2.source='Workday' AND r2.started_at=run_sources.started_at)")
        self.conn.execute("UPDATE run_sources SET source='Workday' WHERE source='workday'")

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
                       posted_at: str | None, snapshot: Any, ts: str, dedupe_key: str | None = None) -> dict:
        """Insert or refresh a posting. Returns {first_seen, is_new}."""
        row = self.conn.execute("SELECT first_seen FROM postings WHERE posting_id=?", (posting_id,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO postings(posting_id, company, title, url, posted_at, first_seen, last_seen, "
                "closed_at, is_new, snapshot, dedupe_key) VALUES (?,?,?,?,?,?,?,NULL,1,?,?)",
                (posting_id, company, title, url, posted_at, ts, ts,
                 json.dumps(snapshot, default=str) if snapshot is not None else None, dedupe_key))
            return {"first_seen": ts, "is_new": True}
        self.conn.execute(
            "UPDATE postings SET title=?, url=?, posted_at=COALESCE(?, posted_at), last_seen=?, "
            "closed_at=NULL, is_new=0, dedupe_key=COALESCE(?, dedupe_key), "
            "snapshot=COALESCE(snapshot, ?) WHERE posting_id=?",
            (title, url, posted_at, ts, dedupe_key,
             json.dumps(snapshot, default=str) if snapshot is not None else None, posting_id))
        return {"first_seen": row["first_seen"], "is_new": False}

    def merge_search_copies(self, board_id: str, dedupe_key: str, ts: str) -> str | None:
        """A company-board posting replaces any search-source copy of the same job.

        Your Decision / Interest / Status / Notes and the earlier first-seen date
        move to the board posting. Returns the merged search posting id, if any.
        """
        row = self.conn.execute(
            "SELECT posting_id, first_seen FROM postings WHERE dedupe_key=? AND posting_id<>? "
            "AND (posting_id LIKE 'muse:%' OR posting_id LIKE 'jsearch:%') AND merged_into IS NULL",
            (dedupe_key, board_id)).fetchone()
        if row is None:
            return None
        self.conn.execute("UPDATE postings SET merged_into=?, closed_at=COALESCE(closed_at, ?) WHERE posting_id=?",
                          (board_id, ts, row["posting_id"]))
        self.conn.execute("UPDATE postings SET first_seen=MIN(first_seen, ?) WHERE posting_id=?",
                          (row["first_seen"], board_id))
        st = self.conn.execute("SELECT * FROM owner_state WHERE posting_id=?", (row["posting_id"],)).fetchone()
        if st is not None and not self.conn.execute(
                "SELECT 1 FROM owner_state WHERE posting_id=?", (board_id,)).fetchone():
            self.conn.execute(
                "INSERT INTO owner_state(posting_id, decision, interest, status, notes, applied_on, "
                "status_changed_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (board_id, st["decision"], st["interest"], st["status"], st["notes"], st["applied_on"],
                 st["status_changed_at"], ts))
        return row["posting_id"]

    def merge_into(self, posting_id: str, keep_id: str) -> None:
        """Hide an already-stored duplicate behind the copy we keep."""
        self.conn.execute("UPDATE postings SET merged_into=? WHERE posting_id=? AND merged_into IS NULL",
                          (keep_id, posting_id))

    def is_merged(self, posting_id: str) -> bool:
        r = self.conn.execute("SELECT merged_into FROM postings WHERE posting_id=?", (posting_id,)).fetchone()
        return r is not None and r["merged_into"] is not None

    def open_board_key(self, dedupe_key: str) -> bool:
        """True if a company-board posting with this key is already stored."""
        return self.conn.execute("SELECT 1 FROM postings WHERE dedupe_key=? AND posting_id NOT LIKE 'muse:%' "
                                 "AND posting_id NOT LIKE 'jsearch:%'", (dedupe_key,)).fetchone() is not None

    def open_key(self, dedupe_key: str, exclude_prefix: str) -> bool:
        """True if another open posting (any source but `exclude_prefix`) has this key."""
        return self.conn.execute("SELECT 1 FROM postings WHERE dedupe_key=? AND posting_id NOT LIKE ? "
                                 "AND closed_at IS NULL AND merged_into IS NULL",
                                 (dedupe_key, exclude_prefix + "%")).fetchone() is not None

    def close_stale(self, prefix: str, days: int, ts: str) -> int:
        """Close open postings from a rotating search not seen for `days` days."""
        cutoff = (datetime.fromisoformat(ts) - timedelta(days=days)).isoformat(timespec="seconds")
        cur = self.conn.execute("UPDATE postings SET closed_at=?, is_new=0 WHERE posting_id LIKE ? "
                                "AND closed_at IS NULL AND last_seen < ?", (ts, prefix + "%", cutoff))
        return cur.rowcount

    def close_missing(self, prefix: str, seen_ids: Iterable[str], ts: str) -> int:
        """Close open postings whose id starts with `prefix` (one board / one source) and weren't seen."""
        seen = set(seen_ids)
        rows = self.conn.execute(
            "SELECT posting_id FROM postings WHERE posting_id LIKE ? AND closed_at IS NULL",
            (prefix.replace("%", "") + "%",)).fetchall()
        missing = [r["posting_id"] for r in rows if r["posting_id"] not in seen]
        self.conn.executemany("UPDATE postings SET closed_at=?, is_new=0 WHERE posting_id=?",
                              [(ts, pid) for pid in missing])
        return len(missing)

    def compact(self) -> None:
        """Drop full copies of postings that fail the rules; reclaim space now and then."""
        self.conn.execute("UPDATE postings SET snapshot=NULL WHERE snapshot IS NOT NULL AND posting_id IN "
                          "(SELECT posting_id FROM scores WHERE passed=0)")
        self.conn.commit()
        if str(self.path) != ":memory:":
            self.conn.execute("VACUUM")

    def mark_evergreen(self, days: int, ts: str) -> None:
        cutoff = (datetime.fromisoformat(ts) - timedelta(days=days)).isoformat(timespec="seconds")
        # the board's posted date when it has one (a 2017 posting is not new just because we found it today)
        self.conn.execute("UPDATE postings SET evergreen = CASE WHEN closed_at IS NULL AND "
                          "MIN(COALESCE(NULLIF(posted_at, ''), first_seen), first_seen) <= ? THEN 1 ELSE 0 END", (cutoff,))

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
            "SELECT p.*, s.* FROM postings p JOIN scores s USING(posting_id) "
            "WHERE s.passed=1 AND p.merged_into IS NULL").fetchall()

    def dropped_rows(self, days: int, ts: str) -> list[sqlite3.Row]:
        cutoff = (datetime.fromisoformat(ts) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.conn.execute(
            "SELECT p.company, p.title, p.url, s.gate, s.matched, s.dropped_at FROM postings p "
            "JOIN scores s USING(posting_id) WHERE s.passed=0 AND s.dropped_at >= ? "
            "ORDER BY s.dropped_at DESC", (cutoff,)).fetchall()

    # --- discovery ----------------------------------------------------------
    def discovered_boards(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM discovered_boards ORDER BY company").fetchall()

    def add_discovered(self, company: str, platform: str, slug: str, via: str, title: str, ts: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO discovered_boards VALUES (?,?,?,?,?,?)",
                          (company, platform, slug, ts, via, title))

    def probe_due(self, company: str, retry_days: int, ts: str) -> bool:
        r = self.conn.execute("SELECT probed_at FROM probes WHERE company=?", (company,)).fetchone()
        if r is None:
            return True
        return datetime.fromisoformat(r["probed_at"]) <= datetime.fromisoformat(ts) - timedelta(days=retry_days)

    def record_probe(self, company: str, result: str, ts: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO probes VALUES (?,?,?)", (company, ts, result))

    def next_pairs(self, source: str, pairs: list[str], n: int) -> list[tuple[str, bool]]:
        """The n least-recently searched pairs, never-searched first. Returns (pair, first_time)."""
        last = {r["pair"]: r["last_searched"] for r in
                self.conn.execute("SELECT pair, last_searched FROM search_pairs WHERE source=?", (source,))}
        order = sorted(pairs, key=lambda p: (p in last, last.get(p) or ""))
        return [(p, p not in last) for p in order[:n]]

    def mark_pair(self, source: str, pair: str, count: int, ts: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO search_pairs VALUES (?,?,?,?)", (source, pair, ts, count))

    def budget_left(self, source: str, per_day: int, ts: str) -> int:
        r = self.conn.execute("SELECT used FROM search_usage WHERE source=? AND day=?", (source, ts[:10])).fetchone()
        return max(0, per_day - (r["used"] if r else 0))

    def spend(self, source: str, n: int, ts: str) -> None:
        self.conn.execute("INSERT INTO search_usage VALUES (?,?,?) ON CONFLICT(source, day) DO UPDATE SET "
                          "used = used + excluded.used", (source, ts[:10], n))

    def record_ats_link(self, url_root: str, platform: str, company: str, ts: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO ats_links VALUES (?,?,?,?)", (url_root, platform, company, ts))

    def count_link_domain(self, domain: str) -> None:
        self.conn.execute("INSERT INTO link_domains VALUES (?, 1) ON CONFLICT(domain) DO UPDATE SET seen = seen + 1",
                          (domain,))

    def source_due(self, source: str, every_hours: float, ts: str) -> bool:
        if os.environ.get("PIPELINE_CHECK_ALL") == "1":
            return True       # manual "check everything now" run (the JSearch budget still applies)
        r = self.conn.execute("SELECT last_complete FROM source_runs WHERE source=?", (source,)).fetchone()
        if r is None or not r["last_complete"]:
            return True
        return datetime.fromisoformat(r["last_complete"]) <= datetime.fromisoformat(ts) - timedelta(hours=every_hours)

    def source_complete(self, source: str, ts: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO source_runs VALUES (?,?)", (source, ts))

    # --- detail cache (SmartRecruiters) ---------------------------------------
    def details_for(self, prefix: str) -> dict[str, dict]:
        rows = self.conn.execute("SELECT posting_id, payload FROM detail_cache WHERE posting_id LIKE ?",
                                 (prefix + "%",)).fetchall()
        return {r["posting_id"][len(prefix):]: json.loads(r["payload"]) for r in rows}

    def save_details(self, prefix: str, details: dict[str, dict], ts: str) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO detail_cache VALUES (?,?,?)",
                              [(prefix + k, json.dumps(v), ts) for k, v in details.items()])

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

    def record_run_sources(self, started_at: str, stats: dict[str, dict]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO run_sources VALUES (?,?,?,?,?,?,?,?)",
            [(started_at, src, s["seen"], s["new"], s["passed"], s["new_passed"], s["seen"] - s["passed"],
              json.dumps(dict(s["reasons"]))) for src, s in stats.items()])

    def latest_source_stats(self) -> list[sqlite3.Row]:
        """Each source's row from the most recent run that checked it."""
        return self.conn.execute(
            "SELECT rs.* FROM run_sources rs JOIN (SELECT source, MAX(started_at) m FROM run_sources GROUP BY source) x "
            "ON rs.source = x.source AND rs.started_at = x.m ORDER BY rs.seen DESC").fetchall()

    def run_sources(self, started_at: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM run_sources WHERE started_at=? ORDER BY seen DESC",
                                 (started_at,)).fetchall()

    def coverage_counts(self, ts: str) -> dict:
        week = (datetime.fromisoformat(ts) - timedelta(days=7)).isoformat(timespec="seconds")
        q = self.conn.execute
        return {
            "discovered_by_platform": {r[0]: r[1] for r in q("SELECT platform, COUNT(*) FROM discovered_boards GROUP BY platform")},
            "discovered_by_source": {(r[0] or "search"): r[1] for r in q("SELECT via, COUNT(*) FROM discovered_boards GROUP BY via")},
            "discovered_this_week": q("SELECT COUNT(*) FROM discovered_boards WHERE found_at >= ?", (week,)).fetchone()[0],
            "jsearch_pairs_searched": q("SELECT COUNT(*) FROM search_pairs WHERE source='jsearch'").fetchone()[0],
            "jsearch_used_today": (q("SELECT used FROM search_usage WHERE source='jsearch' AND day=?",
                                     (ts[:10],)).fetchone() or [0])[0],
            "muse_last_sweep": (q("SELECT last_complete FROM source_runs WHERE source='muse'").fetchone() or [""])[0],
            "unread_sites": [(r[0], r[1]) for r in q("SELECT domain, seen FROM link_domains ORDER BY seen DESC LIMIT 8")],
            "open_options": q("SELECT COUNT(*) FROM postings p JOIN scores s USING(posting_id) WHERE s.passed=1 "
                              "AND p.closed_at IS NULL AND p.merged_into IS NULL").fetchone()[0],
        }

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

    def note_board_passes(self, company: str, passed: int, ts: str) -> None:
        self.conn.execute("UPDATE board_health SET first_polled=COALESCE(first_polled, ?) WHERE company=?", (ts, company))
        if passed:
            self.conn.execute("UPDATE board_health SET last_pass_at=? WHERE company=?", (ts, company))

    def board_is_quiet(self, company: str, days: float, ts: str) -> bool:
        r = self.conn.execute("SELECT first_polled, last_pass_at FROM board_health WHERE company=?",
                              (company,)).fetchone()
        if r is None or not r["first_polled"]:
            return False
        cutoff = datetime.fromisoformat(ts) - timedelta(days=days)
        since = r["last_pass_at"] or r["first_polled"]
        return datetime.fromisoformat(since) <= cutoff

    def board_error(self, company: str, platform: str, error: str, ts: str) -> None:
        self.conn.execute(
            "INSERT INTO board_health(company, platform, last_error, last_error_at, status) VALUES (?,?,?,?,'Error') "
            "ON CONFLICT(company) DO UPDATE SET last_error=excluded.last_error, "
            "last_error_at=excluded.last_error_at, status='Error'", (company, platform, error[:500], ts))

    def board_health(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM board_health ORDER BY company").fetchall()

    def commit(self) -> None:
        self.conn.commit()

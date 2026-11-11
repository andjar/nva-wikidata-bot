"""SQLite state: runs, every action taken, the human review queue and cursors."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, mode TEXT, live INTEGER, batch_id TEXT,
  started TEXT, finished TEXT, status TEXT, counters TEXT);
CREATE TABLE IF NOT EXISTS actions(
  id INTEGER PRIMARY KEY, run_id INTEGER, ts TEXT, qid TEXT, kind TEXT, prop TEXT,
  value TEXT, status TEXT, revid INTEGER, subject TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS actions_status ON actions(status, kind, prop);
CREATE TABLE IF NOT EXISTS review(
  qid TEXT, kind TEXT, prop TEXT, value TEXT, detail TEXT, subject TEXT,
  first_seen TEXT, last_seen TEXT, seen_count INTEGER,
  PRIMARY KEY(qid, kind, prop, value));
CREATE TABLE IF NOT EXISTS cursors(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS created(nva_identifier TEXT PRIMARY KEY, qid TEXT, ts TEXT, run_id INTEGER);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def start_run(self, mode: str, live: bool, batch_id: str) -> int:
        cur = self.db.execute("INSERT INTO runs(mode, live, batch_id, started, status) VALUES(?,?,?,?,?)",
                              (mode, int(live), batch_id, now(), "running"))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, counters: dict) -> None:
        self.db.execute("UPDATE runs SET finished=?, status=?, counters=? WHERE id=?",
                        (now(), status, json.dumps(counters, sort_keys=True), run_id))
        self.db.commit()

    def log_action(self, run_id: int, a, status: str, subject: str = "", revid: Optional[int] = None,
                   message: str = "") -> None:
        self.db.execute("INSERT INTO actions(run_id, ts, qid, kind, prop, value, status, revid, subject, message)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, now(), a.qid, a.kind, a.prop, str(a.value), status, revid, subject,
                         message or a.reason))
        self.db.commit()

    def add_review(self, qid: str, kind: str, prop: str, value: str, detail: str, subject: str) -> None:
        ts = now()
        self.db.execute(
            "INSERT INTO review VALUES(?,?,?,?,?,?,?,?,1) ON CONFLICT(qid, kind, prop, value) DO UPDATE SET "
            "detail=excluded.detail, subject=excluded.subject, last_seen=excluded.last_seen, "
            "seen_count=seen_count+1",
            (qid, kind, prop, str(value), detail, subject, ts, ts))
        self.db.commit()

    def add_created(self, nva_identifier: str, qid: str, run_id: int) -> None:
        """Remember every item we created - WDQS lags, this table does not."""
        self.db.execute("INSERT OR REPLACE INTO created VALUES(?,?,?,?)", (nva_identifier, qid, now(), run_id))
        self.db.commit()

    def created_qid(self, nva_identifier: str) -> Optional[str]:
        row = self.db.execute("SELECT qid FROM created WHERE nva_identifier=?", (nva_identifier,)).fetchone()
        return row["qid"] if row else None

    def get_cursor(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_cursor(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))
        self.db.commit()

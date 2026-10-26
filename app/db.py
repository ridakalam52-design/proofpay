"""Tiny SQLite data layer (no ORM, zero setup)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    id TEXT PRIMARY KEY,
    client_token TEXT UNIQUE NOT NULL,
    worker_token TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    worker_name TEXT,
    task TEXT,
    amount TEXT,
    currency TEXT DEFAULT 'USD',
    deadline TEXT,
    checklist TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    paypal_order_id TEXT,
    authorization_id TEXT,
    capture_id TEXT,
    ai_mode TEXT,
    ai_status TEXT,
    draft_version INTEGER NOT NULL DEFAULT 0,
    clarifications TEXT DEFAULT '[]',
    paypal_mode TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    deal_id TEXT NOT NULL REFERENCES deals(id),
    note TEXT,
    photos TEXT DEFAULT '[]',
    photo_hashes TEXT DEFAULT '[]',
    verdict TEXT,
    decision TEXT,
    status TEXT NOT NULL DEFAULT 'done',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id TEXT NOT NULL REFERENCES deals(id),
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_deal ON events(deal_id);
CREATE INDEX IF NOT EXISTS idx_sub_deal ON submissions(deal_id);
"""

JSON_COLS = {"checklist", "photos", "photo_hashes", "verdict", "data", "clarifications"}

# Columns added after the first release: (table, column, DDL) - applied to older databases on startup.
MIGRATIONS = [
    ("deals", "ai_status", "ALTER TABLE deals ADD COLUMN ai_status TEXT"),
    ("deals", "draft_version", "ALTER TABLE deals ADD COLUMN draft_version INTEGER NOT NULL DEFAULT 0"),
    ("deals", "clarifications", "ALTER TABLE deals ADD COLUMN clarifications TEXT DEFAULT '[]'"),
    ("submissions", "status", "ALTER TABLE submissions ADD COLUMN status TEXT NOT NULL DEFAULT 'done'"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            for table, col, ddl in MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    self._conn.execute(ddl)
            self._conn.commit()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for k in JSON_COLS & out.keys():
            if out[k] is not None:
                try:
                    out[k] = json.loads(out[k])
                except (TypeError, ValueError):
                    pass
        return out

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            return self._row(self._conn.execute(sql, params).fetchone())

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._conn.execute(sql, params).fetchall()]

    # -- deals -------------------------------------------------------------
    def insert_deal(self, deal: dict) -> None:
        deal = {**deal, "checklist": json.dumps(deal.get("checklist", [])),
                "clarifications": json.dumps(deal.get("clarifications", []))}
        cols = ", ".join(deal)
        qs = ", ".join("?" for _ in deal)
        self.execute(f"INSERT INTO deals ({cols}) VALUES ({qs})", tuple(deal.values()))

    def update_deal(self, deal_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields = self._enc(fields)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE deals SET {sets} WHERE id = ?", (*fields.values(), deal_id))

    @staticmethod
    def _enc(fields: dict) -> dict:
        fields = {**fields, "updated_at": now_iso()}
        for k in ("checklist", "clarifications"):
            if k in fields:
                fields[k] = json.dumps(fields[k])
        return fields

    def edit_draft(self, deal_id: str, **fields: Any) -> bool:
        """Client edit: only while still a draft; bumps draft_version so background AI won't overwrite it."""
        fields = self._enc(fields)
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = self.execute(
            f"UPDATE deals SET {sets}, draft_version = draft_version + 1 WHERE id = ? AND status = 'draft'",
            (*fields.values(), deal_id),
        )
        return cur.rowcount == 1

    def apply_refinement(self, deal_id: str, expected_version: int, **fields: Any) -> bool:
        """Background AI parse result: applied only if still a draft the client hasn't edited since."""
        fields = self._enc(fields)
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = self.execute(
            f"UPDATE deals SET {sets} WHERE id = ? AND status = 'draft' AND draft_version = ?",
            (*fields.values(), deal_id, expected_version),
        )
        return cur.rowcount == 1

    def deals_with_ai_status(self, ai_status: str) -> list[dict]:
        return self.all("SELECT * FROM deals WHERE ai_status = ?", (ai_status,))

    def transition(self, deal_id: str, from_statuses: tuple[str, ...], to_status: str) -> bool:
        """Atomic compare-and-set on status. Returns False if the deal moved on."""
        qs = ", ".join("?" for _ in from_statuses)
        cur = self.execute(
            f"UPDATE deals SET status = ?, updated_at = ? WHERE id = ? AND status IN ({qs})",
            (to_status, now_iso(), deal_id, *from_statuses),
        )
        return cur.rowcount == 1

    def deal_by(self, column: str, value: str) -> dict | None:
        assert column in {"id", "client_token", "worker_token"}
        return self.one(f"SELECT * FROM deals WHERE {column} = ?", (value,))

    # -- submissions -------------------------------------------------------
    def insert_submission(self, sub: dict) -> None:
        sub = {
            **sub,
            "photos": json.dumps(sub.get("photos", [])),
            "photo_hashes": json.dumps(sub.get("photo_hashes", [])),
            "verdict": json.dumps(sub.get("verdict")) if sub.get("verdict") is not None else None,
        }
        cols = ", ".join(sub)
        qs = ", ".join("?" for _ in sub)
        self.execute(f"INSERT INTO submissions ({cols}) VALUES ({qs})", tuple(sub.values()))

    def update_submission(self, sub_id: str, verdict: dict, decision: str, status: str = "done") -> None:
        self.execute(
            "UPDATE submissions SET verdict = ?, decision = ?, status = ? WHERE id = ?",
            (json.dumps(verdict), decision, status, sub_id),
        )

    def claim_submission(self, sub_id: str, from_status: str, to_status: str) -> bool:
        """Atomic compare-and-set on a submission's status (guards against applying a verdict twice)."""
        cur = self.execute("UPDATE submissions SET status = ? WHERE id = ? AND status = ?",
                           (to_status, sub_id, from_status))
        return cur.rowcount == 1

    def submission(self, sub_id: str) -> dict | None:
        return self.one("SELECT * FROM submissions WHERE id = ?", (sub_id,))

    def submissions_with_status(self, status: str) -> list[dict]:
        return self.all("SELECT * FROM submissions WHERE status = ?", (status,))

    def submissions(self, deal_id: str) -> list[dict]:
        return self.all("SELECT * FROM submissions WHERE deal_id = ? ORDER BY created_at, rowid", (deal_id,))

    def hash_seen_elsewhere(self, sha: str, deal_id: str) -> bool:
        rows = self.all("SELECT photo_hashes FROM submissions WHERE deal_id != ?", (deal_id,))
        return any(sha in (r["photo_hashes"] or []) for r in rows)

    def hash_seen_in_deal(self, sha: str, deal_id: str) -> bool:
        rows = self.all("SELECT photo_hashes FROM submissions WHERE deal_id = ?", (deal_id,))
        return any(sha in (r["photo_hashes"] or []) for r in rows)

    # -- events ------------------------------------------------------------
    def log(self, deal_id: str, actor: str, kind: str, message: str, data: dict | None = None) -> None:
        self.execute(
            "INSERT INTO events (deal_id, ts, actor, kind, message, data) VALUES (?, ?, ?, ?, ?, ?)",
            (deal_id, now_iso(), actor, kind, message, json.dumps(data) if data else None),
        )

    def events(self, deal_id: str) -> list[dict]:
        return self.all("SELECT * FROM events WHERE deal_id = ? ORDER BY id", (deal_id,))

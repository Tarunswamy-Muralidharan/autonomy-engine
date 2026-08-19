"""SQLite implementation of the Repo interface (local dev + tests).

Thread-safe via a single lock: SQLite is not the concurrency story here
(DynamoDB is, in production); the lock just keeps local dev correct.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from .repo import Repo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    action_id   TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    ts          REAL NOT NULL,
    payload     TEXT NOT NULL,          -- full record as JSON
    outcome     TEXT NOT NULL DEFAULT 'pending',
    decided_by  TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit(agent_id);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- confirm | review
    status      TEXT NOT NULL,          -- pending | approved | rejected
    ts          REAL NOT NULL,
    payload     TEXT NOT NULL,
    decided_at  REAL,
    decided_by  TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

CREATE TABLE IF NOT EXISTS calibration (
    action_type   TEXT PRIMARY KEY,
    confirms      INTEGER NOT NULL DEFAULT 0,
    approvals     INTEGER NOT NULL DEFAULT 0,
    rejections    INTEGER NOT NULL DEFAULT 0,
    modifications INTEGER NOT NULL DEFAULT 0,
    adjustment    REAL NOT NULL DEFAULT 0
);
"""


class SqliteRepo(Repo):
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # --- audit ---
    def put_audit(self, record: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (action_id, session_id, agent_id, ts, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (record["action_id"], record["session_id"], record["agent_id"],
                 record["ts"], json.dumps(record)),
            )
            self._conn.commit()

    def update_audit_outcome(self, action_id: str, outcome: str, decided_by: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE audit SET outcome = ?, decided_by = ? WHERE action_id = ?",
                (outcome, decided_by, action_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def query_audit(self, session_id=None, agent_id=None, limit=100):
        q = "SELECT payload, outcome, decided_by FROM audit"
        conds, args = [], []
        if session_id:
            conds.append("session_id = ?"); args.append(session_id)
        if agent_id:
            conds.append("agent_id = ?"); args.append(agent_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            rec = json.loads(r["payload"])
            rec["final_outcome"] = r["outcome"]
            rec["decided_by"] = r["decided_by"]
            out.append(rec)
        return out

    # --- tickets ---
    def put_ticket(self, ticket: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tickets (ticket_id, kind, status, ts, payload) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (ticket["ticket_id"], ticket["kind"], ticket["ts"], json.dumps(ticket)),
            )
            self._conn.commit()

    def get_ticket(self, ticket_id: str):
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
        return self._row_to_ticket(r) if r else None

    def decide_ticket(self, ticket_id: str, decision: str, decided_by: str, note: str = ""):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tickets SET status = ?, decided_at = ?, decided_by = ?, note = ? "
                "WHERE ticket_id = ? AND status = 'pending'",
                (decision, time.time(), decided_by, note, ticket_id),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            r = self._conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
        return self._row_to_ticket(r)

    def list_tickets(self, status=None, kind=None):
        q, conds, args = "SELECT * FROM tickets", [], []
        if status:
            conds.append("status = ?"); args.append(status)
        if kind:
            conds.append("kind = ?"); args.append(kind)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts DESC"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    @staticmethod
    def _row_to_ticket(r) -> dict:
        t = json.loads(r["payload"])
        t.update(status=r["status"], decided_at=r["decided_at"],
                 decided_by=r["decided_by"], note=r["note"])
        return t

    # --- calibration ---
    def get_calibration(self, action_type: str) -> dict:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM calibration WHERE action_type = ?", (action_type,)
            ).fetchone()
        if not r:
            return {"action_type": action_type, "confirms": 0, "approvals": 0,
                    "rejections": 0, "adjustment": 0.0}
        return dict(r)

    def update_calibration(self, action_type: str, decision: str) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO calibration (action_type) VALUES (?)",
                (action_type,),
            )
            col = {"approved": "approvals", "rejected": "rejections",
                   "modified": "modifications"}.get(decision, "rejections")
            self._conn.execute(
                f"UPDATE calibration SET confirms = confirms + 1, {col} = {col} + 1 "
                "WHERE action_type = ?",
                (action_type,),
            )
            self._conn.commit()
        return self.get_calibration(action_type)

    def set_calibration_adjustment(self, action_type: str, adjustment: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE calibration SET adjustment = ? WHERE action_type = ?",
                (adjustment, action_type),
            )
            self._conn.commit()

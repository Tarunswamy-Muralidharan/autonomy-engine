"""Cumulative-action ledger: per-agent counters across time windows.

Why counters and not a log scan: the check runs on EVERY /evaluate call, so
it must be a single keyed read, never a query over the audit table - an
audit-scan approach would slow down (and eventually time out) exactly as the
attack it guards against progresses.

Why increments must be atomic: two concurrent Lambda instances recording the
50th and 51st delete must never read-modify-write each other's update away.
A lost increment UNDER-counts, and an under-counting ledger is a fail-open
red line - the one direction this module must never fail in. SQLite uses a
single relative UPDATE under the connection lock; DynamoDB uses ADD, the same
pattern as update_calibration in app/storage/dynamo_repo.py.

Backend selection mirrors app/storage/repo.py (env-switched singleton) but is
module-local so this package needs no edits to existing files.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone

# Sentinel tool name for the per-minute proposal counter. "*" cannot collide
# with a real tool: normalize_tool() lowercases and strips, but the engine's
# policy vocabulary never contains "*", and even if a caller sent tool="*"
# the collision would only OVER-count velocity - which fails toward CONFIRM,
# the safe direction.
VELOCITY_TOOL = "*"

# TTLs for the DynamoDB expires_at attribute so the table self-cleans.
# Windows outlive their usefulness quickly; keeping them forever would grow
# the table (and the attack surface for a hot-partition DoS) without bound.
SESSION_TTL_SECONDS = 7 * 24 * 3600   # sessions can idle across days
DAY_TTL_SECONDS = 48 * 3600           # day bucket + a full day of clock skew
MINUTE_TTL_SECONDS = 3600             # velocity is only meaningful for minutes


def day_bucket(ts: float) -> str:
    """UTC date bucket "YYYY-MM-DD". UTC, never local time: an agent must not
    be able to reset its daily budget by racing a timezone boundary, and
    Lambda regions must all agree on which day it is."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def minute_bucket(ts: float) -> str:
    """UTC minute bucket "YYYY-MM-DDTHH:MM" for velocity counting."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")


class LedgerRepo(ABC):
    """Storage interface. record_action/get_velocity are concrete so BOTH
    backends share identical window semantics - a drift between SQLite and
    DynamoDB bucketing would make tests green locally and the red line
    porous in production."""

    @abstractmethod
    def _increment(self, tenant_id: str, agent_id: str, window_kind: str,
                   window_id: str, tool: str, calls: int, affected: int,
                   expires_at: int) -> None:
        """Atomically add (calls, affected) to one counter row."""

    @abstractmethod
    def get_totals(self, tenant_id: str, agent_id: str, window_kind: str,
                   window_id: str, tool: str) -> dict:
        """Always the full shape {"calls": int, "affected": int}, zeros if the
        counter has never been touched - same discipline as get_calibration,
        so callers never branch on missing keys."""

    def record_action(self, tenant_id: str, agent_id: str, session_id: str,
                      tool: str, affected_count: int, ts: float) -> None:
        """Record one evaluated action in every window it belongs to.

        Called for EVERY evaluation regardless of route: an agent that got 99
        AUTONOMOUS single-record deletes through must find the 100th already
        counted against it. Negative affected_count is clamped to 0 so a
        crafted negative value cannot DECREMENT the running total (that would
        be a ledger-poisoning bypass).
        """
        affected = max(0, int(affected_count))
        now = float(ts)
        self._increment(tenant_id, agent_id, "session", session_id, tool,
                        1, affected, int(now) + SESSION_TTL_SECONDS)
        self._increment(tenant_id, agent_id, "day", day_bucket(now), tool,
                        1, affected, int(now) + DAY_TTL_SECONDS)
        # Velocity counts proposals per (tenant, agent, minute) across ALL
        # tools: a flood of 100 different cheap tools is the same DoS as a
        # flood of one.
        self._increment(tenant_id, agent_id, "minute", minute_bucket(now),
                        VELOCITY_TOOL, 1, 0, int(now) + MINUTE_TTL_SECONDS)

    def get_velocity(self, tenant_id: str, agent_id: str, ts: float) -> int:
        """Proposals recorded in the current minute bucket."""
        return self.get_totals(tenant_id, agent_id, "minute",
                               minute_bucket(float(ts)), VELOCITY_TOOL)["calls"]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    tenant_id   TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    window_kind TEXT NOT NULL,          -- session | day | minute
    window_id   TEXT NOT NULL,          -- session_id | YYYY-MM-DD | YYYY-MM-DDTHH:MM
    tool        TEXT NOT NULL,          -- '*' for the velocity counter
    calls       INTEGER NOT NULL DEFAULT 0,
    affected    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, agent_id, window_kind, window_id, tool)
);
"""


class SqliteLedgerRepo(LedgerRepo):
    """Local dev + tests. Thread-safe via a single lock, same rationale as
    app/storage/sqlite_repo.py: DynamoDB is the concurrency story in
    production, the lock keeps local totals exact."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _increment(self, tenant_id, agent_id, window_kind, window_id, tool,
                   calls, affected, expires_at) -> None:
        # expires_at is intentionally unused: the local dev database is
        # disposable; TTL is a DynamoDB cost/hygiene concern.
        #
        # INSERT OR IGNORE then a single RELATIVE UPDATE under the lock (the
        # update_calibration pattern): the database computes x + delta, so no
        # stale value read by this process can ever be written back.
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO ledger "
                "(tenant_id, agent_id, window_kind, window_id, tool) "
                "VALUES (?, ?, ?, ?, ?)",
                (tenant_id, agent_id, window_kind, window_id, tool),
            )
            self._conn.execute(
                "UPDATE ledger SET calls = calls + ?, affected = affected + ? "
                "WHERE tenant_id = ? AND agent_id = ? AND window_kind = ? "
                "AND window_id = ? AND tool = ?",
                (calls, affected, tenant_id, agent_id, window_kind, window_id, tool),
            )
            self._conn.commit()

    def get_totals(self, tenant_id, agent_id, window_kind, window_id, tool) -> dict:
        with self._lock:
            r = self._conn.execute(
                "SELECT calls, affected FROM ledger "
                "WHERE tenant_id = ? AND agent_id = ? AND window_kind = ? "
                "AND window_id = ? AND tool = ?",
                (tenant_id, agent_id, window_kind, window_id, tool),
            ).fetchone()
        if not r:
            return {"calls": 0, "affected": 0}
        return {"calls": int(r["calls"]), "affected": int(r["affected"])}


class DynamoLedgerRepo(LedgerRepo):
    """Production backend.

    Table f"{PREFIX}-ledger" (create with scripts/create_tables.py):
      pk (S)  "tenant#agent#windowkind#windowid"   HASH
      sk (S)  tool                                 RANGE
      expires_at (N) epoch seconds - enable TTL on this attribute so the
      table self-cleans (see docs/integration/cumulative.md).

    A "#" inside tenant_id/agent_id could make two composite keys collide,
    which would MERGE counters and over-count - that fails toward review,
    never away from it, so it is accepted rather than escaped.
    """

    def __init__(self):
        import boto3  # deferred so the sqlite path never needs AWS deps at import

        # Read env at construction (not module import) so reset_ledger_repo()
        # honours per-test environment changes.
        prefix = os.environ.get("AUTONOMYGATE_TABLE_PREFIX", "autonomygate")
        region = os.environ.get("AWS_REGION", "us-east-1")
        ddb = boto3.resource("dynamodb", region_name=region)
        self.table = ddb.Table(f"{prefix}-ledger")

    @staticmethod
    def _pk(tenant_id, agent_id, window_kind, window_id) -> str:
        return f"{tenant_id}#{agent_id}#{window_kind}#{window_id}"

    def _increment(self, tenant_id, agent_id, window_kind, window_id, tool,
                   calls, affected, expires_at) -> None:
        # ADD is atomic server-side (same pattern as update_calibration):
        # concurrent Lambda instances can never lose each other's increment.
        # expires_at is written once (if_not_exists) so repeated activity in a
        # window does not keep pushing its expiry forward.
        self.table.update_item(
            Key={"pk": self._pk(tenant_id, agent_id, window_kind, window_id),
                 "sk": tool},
            UpdateExpression="ADD calls :c, affected :a "
                             "SET expires_at = if_not_exists(expires_at, :exp)",
            ExpressionAttributeValues={":c": int(calls), ":a": int(affected),
                                       ":exp": int(expires_at)},
        )

    def get_totals(self, tenant_id, agent_id, window_kind, window_id, tool) -> dict:
        # Strongly consistent: the 101st delete must see all 100 already
        # recorded. An eventually-consistent read could miss the most recent
        # increments and re-open the exact hole this module closes.
        item = self.table.get_item(
            Key={"pk": self._pk(tenant_id, agent_id, window_kind, window_id),
                 "sk": tool},
            ConsistentRead=True,
        ).get("Item")
        if not item:
            return {"calls": 0, "affected": 0}
        # DynamoDB numbers come back as Decimal; counters are always whole.
        return {"calls": int(item.get("calls", 0)),
                "affected": int(item.get("affected", 0))}


_ledger: LedgerRepo | None = None


def get_ledger_repo() -> LedgerRepo:
    """Singleton chosen by environment: AUTONOMYGATE_STORAGE=dynamo|sqlite
    (same switch as app/storage/repo.py, so both stores always agree on the
    backend)."""
    global _ledger
    if _ledger is None:
        backend = os.environ.get("AUTONOMYGATE_STORAGE", "sqlite").lower()
        if backend == "dynamo":
            _ledger = DynamoLedgerRepo()
        else:
            _ledger = SqliteLedgerRepo(
                os.environ.get("AUTONOMYGATE_DB", "autonomygate.db"))
    return _ledger


def reset_ledger_repo() -> None:
    """Test hook: forget the singleton so tests can use a fresh database."""
    global _ledger
    _ledger = None

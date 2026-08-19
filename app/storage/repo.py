"""Storage interface. Two implementations:

  SqliteRepo  - local development and tests (zero AWS dependency)
  DynamoRepo  - production on AWS (added at deploy stage)

The rest of the app only ever imports `get_repo()`, so swapping backends
is an environment variable, not a code change.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any


class Repo(ABC):
    # --- audit log (append-only) ---
    @abstractmethod
    def put_audit(self, record: dict) -> None: ...

    @abstractmethod
    def update_audit_outcome(self, action_id: str, outcome: str, decided_by: str) -> bool: ...

    @abstractmethod
    def get_audit(self, action_id: str) -> dict | None:
        """Keyed lookup of ONE audit record. Never a scan - the outcome
        endpoint must resolve any action regardless of audit-log size."""

    @abstractmethod
    def query_audit(self, session_id: str | None = None, agent_id: str | None = None,
                    limit: int = 100) -> list[dict]: ...

    # --- confirmation / review tickets ---
    @abstractmethod
    def put_ticket(self, ticket: dict) -> None: ...

    @abstractmethod
    def get_ticket(self, ticket_id: str) -> dict | None: ...

    @abstractmethod
    def decide_ticket(self, ticket_id: str, decision: str, decided_by: str,
                      note: str = "") -> dict | None: ...

    @abstractmethod
    def list_tickets(self, status: str | None = None, kind: str | None = None) -> list[dict]: ...

    # --- calibration stats (bonus) ---
    @abstractmethod
    def get_calibration(self, action_type: str) -> dict: ...

    @abstractmethod
    def update_calibration(self, action_type: str, decision: str) -> dict: ...


_repo: Repo | None = None


def get_repo() -> Repo:
    """Singleton repo chosen by environment: AUTONOMYGATE_STORAGE=dynamo|sqlite."""
    global _repo
    if _repo is None:
        backend = os.environ.get("AUTONOMYGATE_STORAGE", "sqlite").lower()
        if backend == "dynamo":
            from .dynamo_repo import DynamoRepo
            _repo = DynamoRepo()
        else:
            from .sqlite_repo import SqliteRepo
            _repo = SqliteRepo(os.environ.get("AUTONOMYGATE_DB", "autonomygate.db"))
    return _repo


def reset_repo() -> None:
    """Test hook: forget the singleton so tests can use a fresh database."""
    global _repo
    _repo = None

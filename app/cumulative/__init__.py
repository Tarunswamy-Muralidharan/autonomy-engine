"""Cumulative blast-radius governance (session & rolling-window ledger).

Closes the salami-slicing hole: the hard overrides fire on ONE delete of 101
records, but 100 single-record deletes each look safe in isolation because the
gate evaluates actions individually. This package accumulates per-agent
totals across windows (session, UTC day, minute) so the 101st slice is judged
against everything that came before it - and so a proposal flood (velocity)
is itself a governed signal.
"""
from .ledger import (
    LedgerRepo,
    day_bucket,
    get_ledger_repo,
    minute_bucket,
    reset_ledger_repo,
)
from .rules import (
    DEFAULT_MAX_PROPOSALS_PER_MINUTE,
    DEFAULT_RULES,
    VELOCITY_REASON,
    VELOCITY_ROUTE,
    CumulativeRule,
    CumulativeVerdict,
    check,
    parse_rules,
    velocity_check,
)

__all__ = [
    "LedgerRepo", "get_ledger_repo", "reset_ledger_repo",
    "day_bucket", "minute_bucket",
    "CumulativeRule", "CumulativeVerdict", "parse_rules", "check",
    "velocity_check", "DEFAULT_RULES", "DEFAULT_MAX_PROPOSALS_PER_MINUTE",
    "VELOCITY_ROUTE", "VELOCITY_REASON",
]

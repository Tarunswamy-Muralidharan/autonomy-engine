"""Tests for app/cumulative - the salami-slicing / velocity red lines.

The scenario that motivates all of this: the 101-record bulk delete is
caught by a hard override, so an agent issues 101 single-record deletes
instead. Each one alone is safe; the sum is the exact catastrophe the
override exists to prevent. These tests prove the ledger + rules close that
hole at the precise boundary, fail closed when storage misbehaves, and
count exactly under concurrency.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.cumulative.ledger import day_bucket, get_ledger_repo, reset_ledger_repo
from app.cumulative.rules import (
    DEFAULT_RULES,
    check,
    parse_rules,
    velocity_check,
)


@pytest.fixture()
def ledger(tmp_path):
    os.environ["AUTONOMYGATE_STORAGE"] = "sqlite"
    os.environ["AUTONOMYGATE_DB"] = str(tmp_path / "cumulative.db")
    reset_ledger_repo()
    yield get_ledger_repo()
    reset_ledger_repo()


def _lookup(ledger, tenant, agent, session_id, ts):
    """The service-side closure: resolves window ids for the current call."""
    def totals_lookup(window_kind, tool):
        window_id = session_id if window_kind == "session" else day_bucket(ts)
        return ledger.get_totals(tenant, agent, window_kind, window_id, tool)
    return totals_lookup


# === THE salami-slicing scenario ===

def test_salami_slicing_101st_single_record_delete_fires(ledger):
    """100 single-record deletes flow normally; the 101st fires the
    session budget - the exact boundary, not off by one in either
    direction."""
    rules = parse_rules(DEFAULT_RULES)
    ts = time.time()
    lookup = _lookup(ledger, "t1", "agent-1", "s1", ts)

    for i in range(100):
        # Before recording delete i+1 the session holds i records; with the
        # pending one that is i+1 <= 100, inside budget.
        assert check(rules, "db_delete", lookup, 1) is None, \
            f"budget fired early at delete #{i + 1}"
        ledger.record_action("t1", "agent-1", "s1", "db_delete", 1, ts)

    verdict = check(rules, "db_delete", lookup, 1)   # the 101st slice
    assert verdict is not None
    assert verdict.name == "bulk_delete_per_session"
    assert verdict.route == "REVIEW"

    # A fresh session starts with a fresh budget...
    fresh = _lookup(ledger, "t1", "agent-1", "s2", ts)
    assert check(rules, "db_delete", fresh, 1) is None
    # ...and another agent's history never bleeds over.
    other = _lookup(ledger, "t1", "agent-2", "s1", ts)
    assert check(rules, "db_delete", other, 1) is None


def test_one_big_pending_delete_also_counts_prior_slices(ledger):
    """50 recorded + a pending 51-record delete = 101 > 100: mixing slice
    sizes cannot dodge the budget."""
    rules = parse_rules(DEFAULT_RULES)
    ts = time.time()
    lookup = _lookup(ledger, "t1", "agent-1", "s1", ts)
    for _ in range(50):
        ledger.record_action("t1", "agent-1", "s1", "db_delete", 1, ts)
    assert check(rules, "db_delete", lookup, 50) is None          # exactly 100
    verdict = check(rules, "db_delete", lookup, 51)               # 101
    assert verdict is not None and verdict.name == "bulk_delete_per_session"


# === day window accumulates across sessions ===

def test_day_window_accumulates_across_sessions(ledger):
    """Per-session budgets reset on a new session; the day budget must not,
    or 'start a new session' becomes the trivial bypass."""
    rules = parse_rules([{
        "name": "small_daily_budget", "tool": "db_delete", "window": "day",
        "metric": "affected", "max": 10, "route": "REVIEW", "reason": "test",
    }])
    ts = time.time()
    for _ in range(3):                                            # session A: 6
        ledger.record_action("t1", "agent-1", "sA", "db_delete", 2, ts)
    for _ in range(2):                                            # session B: 4
        ledger.record_action("t1", "agent-1", "sB", "db_delete", 2, ts)

    lookup = _lookup(ledger, "t1", "agent-1", "sC", ts)           # a THIRD session
    day = ledger.get_totals("t1", "agent-1", "day", day_bucket(ts), "db_delete")
    assert day == {"calls": 5, "affected": 10}
    verdict = check(rules, "db_delete", lookup, 1)                # 10 + 1 > 10
    assert verdict is not None and verdict.name == "small_daily_budget"


# === velocity ===

def test_velocity_fires_at_the_boundary(ledger):
    """With max=3/minute: the 3rd proposal passes, the 4th fires. The next
    minute bucket starts clean."""
    ts = time.time()
    for _ in range(2):
        ledger.record_action("t1", "agent-1", "s1", "crm_read", 1, ts)
    assert ledger.get_velocity("t1", "agent-1", ts) == 2
    assert velocity_check(3, ledger.get_velocity("t1", "agent-1", ts)) is False

    ledger.record_action("t1", "agent-1", "s1", "crm_read", 1, ts)
    assert ledger.get_velocity("t1", "agent-1", ts) == 3
    assert velocity_check(3, ledger.get_velocity("t1", "agent-1", ts)) is True

    assert ledger.get_velocity("t1", "agent-1", ts + 60) == 0     # fresh bucket
    assert velocity_check(3, 0) is False
    assert velocity_check(3, "garbage") is True                   # fail closed


def test_velocity_counts_across_tools(ledger):
    """A flood of many different cheap tools is the same DoS as one tool."""
    ts = time.time()
    for tool in ("crm_read", "crm_search", "crm_update"):
        ledger.record_action("t1", "agent-1", "s1", tool, 1, ts)
    assert ledger.get_velocity("t1", "agent-1", ts) == 3


# === atomicity ===

def test_concurrent_increments_are_exact(ledger):
    """Two threads hammer the same counters; a single lost increment means
    the ledger under-counts, which is a fail-open red line."""
    ts = time.time()
    n, per_thread = 2, 200

    def worker():
        for _ in range(per_thread):
            ledger.record_action("t1", "agent-1", "s1", "db_delete", 3, ts)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    session = ledger.get_totals("t1", "agent-1", "session", "s1", "db_delete")
    assert session == {"calls": n * per_thread, "affected": n * per_thread * 3}
    day = ledger.get_totals("t1", "agent-1", "day", day_bucket(ts), "db_delete")
    assert day == {"calls": n * per_thread, "affected": n * per_thread * 3}
    assert ledger.get_velocity("t1", "agent-1", ts) == n * per_thread


# === fail closed ===

def test_broken_totals_lookup_fires_the_rule():
    """An unreadable ledger must route to a human, not silently wave the
    action through - same construction as _override_matches."""
    rules = parse_rules(DEFAULT_RULES)

    def exploding(window_kind, tool):
        raise RuntimeError("dynamo timeout")

    verdict = check(rules, "db_delete", exploding, 1)
    assert verdict is not None and verdict.name == "bulk_delete_per_session"

    def malformed(window_kind, tool):
        return {"bogus": 1}                        # missing calls/affected

    verdict = check(rules, "db_delete", malformed, 1)
    assert verdict is not None

    def uninterpretable(window_kind, tool):
        return {"calls": "NaN", "affected": "NaN"}

    verdict = check(rules, "db_delete", uninterpretable, 1)
    assert verdict is not None

    # ...but a broken lookup for tool X must not fire rules for tool Y.
    assert check(rules, "crm_read", exploding, 1) is None


def test_negative_pending_cannot_buy_back_budget(ledger):
    """A crafted negative affected_count must neither decrement the ledger
    nor offset the pending contribution."""
    rules = parse_rules(DEFAULT_RULES)
    ts = time.time()
    for _ in range(100):
        ledger.record_action("t1", "agent-1", "s1", "db_delete", 1, ts)
    ledger.record_action("t1", "agent-1", "s1", "db_delete", -50, ts)   # clamped
    session = ledger.get_totals("t1", "agent-1", "session", "s1", "db_delete")
    assert session["affected"] == 100                                   # not 50

    # Push the recorded total over budget, then try to hide behind a
    # negative pending count: naive arithmetic (101 - 5 = 96) would pass,
    # the clamp (101 + 0 = 101 > 100) fires.
    ledger.record_action("t1", "agent-1", "s1", "db_delete", 1, ts)
    lookup = _lookup(ledger, "t1", "agent-1", "s1", ts)
    verdict = check(rules, "db_delete", lookup, -5)
    assert verdict is not None
    assert verdict.name == "bulk_delete_per_session"


# === parse_rules validation ===

def test_parse_rules_accepts_defaults_and_preserves_fields():
    rules = parse_rules(DEFAULT_RULES)
    assert [r.name for r in rules] == ["bulk_delete_per_session", "deletes_per_day"]
    assert rules[0].window == "session" and rules[0].metric == "affected"
    assert rules[0].max == 100 and rules[0].route == "REVIEW"


def test_parse_rules_rejects_malformed_rules():
    base = dict(DEFAULT_RULES[0])

    with pytest.raises(ValueError):                    # missing keys
        parse_rules([{"name": "x"}])
    with pytest.raises(ValueError):                    # unknown window
        parse_rules([{**base, "window": "week"}])
    with pytest.raises(ValueError):                    # unknown metric
        parse_rules([{**base, "metric": "bytes"}])
    with pytest.raises(ValueError):                    # cumulative rules only escalate
        parse_rules([{**base, "route": "AUTONOMOUS"}])
    with pytest.raises(ValueError):                    # duplicate names
        parse_rules([base, base])
    with pytest.raises(ValueError):                    # stringly-typed max
        parse_rules([{**base, "max": "100"}])
    with pytest.raises(ValueError):                    # negative budget
        parse_rules([{**base, "max": -1}])
    with pytest.raises(ValueError):                    # bool masquerading as int
        parse_rules([{**base, "max": True}])


# === get_totals shape discipline ===

def test_get_totals_full_shape_when_absent(ledger):
    assert ledger.get_totals("t", "a", "session", "nope", "db_delete") == \
        {"calls": 0, "affected": 0}

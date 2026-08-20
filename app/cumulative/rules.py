"""Cumulative rules: pure decision logic over ledger totals.

These are red lines in the same sense as policy.yaml hard_overrides - they
route deterministically and bypass the scorer. The difference is WHAT they
look at: an override judges one action in isolation; a cumulative rule judges
the action PLUS everything the agent already did in the window. That closes
the salami-slicing hole where 100 individually-safe single-record deletes add
up to the exact bulk delete the overrides exist to catch.

This module does no I/O: the caller supplies a totals_lookup so the logic is
testable without a database, and so a storage failure has exactly one,
deliberate meaning here - the rule FIRES (fail closed, same philosophy as
_override_matches in app/engine/router.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..engine.router import normalize_tool

_WINDOWS = ("session", "day")
_METRICS = ("affected", "calls")
# AUTONOMOUS is deliberately not a valid route: a cumulative rule may only
# ESCALATE. A rule that could grant autonomy based on accumulated history
# would be a trust back-door, the mirror image of the calibration asymmetry
# enforced in app/engine/router.py.
_ROUTES = ("REVIEW", "CONFIRM")


@dataclass(frozen=True)
class CumulativeRule:
    """One windowed budget, mirroring the shape of a hard_overrides entry."""
    name: str
    tool: str
    window: str    # "session" | "day"
    metric: str    # "affected" | "calls"
    max: int       # budget; the rule fires when total-including-current EXCEEDS it
    route: str     # "REVIEW" | "CONFIRM"
    reason: str


@dataclass(frozen=True)
class CumulativeVerdict:
    """Verdict-shaped result: which rule fired and why. The service stamps
    `name` into the audit record as matched_override so a cumulative match is
    audited exactly like a hard-override match."""
    name: str
    route: str
    reason: str


def parse_rules(raw: list[dict]) -> list[CumulativeRule]:
    """Load-time validation in the spirit of Policy._validate: a typo in a
    rule must refuse to start the service rather than silently never fire -
    a rule that never fires is a fail-OPEN hole in a red line."""
    required = {"name", "tool", "window", "metric", "max", "route", "reason"}
    rules: list[CumulativeRule] = []
    seen: set[str] = set()
    for i, rule in enumerate(raw):
        missing = required - rule.keys()
        if missing:
            raise ValueError(f"cumulative_rules[{i}] missing {sorted(missing)}")
        if rule["name"] in seen:
            raise ValueError(f"duplicate cumulative rule name: {rule['name']}")
        seen.add(rule["name"])
        if rule["window"] not in _WINDOWS:
            raise ValueError(f"{rule['name']}: invalid window {rule['window']!r} "
                             f"(expected one of {_WINDOWS})")
        if rule["metric"] not in _METRICS:
            raise ValueError(f"{rule['name']}: invalid metric {rule['metric']!r} "
                             f"(expected one of {_METRICS})")
        if rule["route"] not in _ROUTES:
            raise ValueError(f"{rule['name']}: invalid route {rule['route']!r} "
                             f"(cumulative rules may only escalate: {_ROUTES})")
        # bool is an int subclass; max=True passing as 1 would be a silent typo.
        if isinstance(rule["max"], bool) or not isinstance(rule["max"], int) \
                or rule["max"] < 0:
            raise ValueError(f"{rule['name']}: max must be a non-negative "
                             f"integer, got {rule['max']!r}")
        rules.append(CumulativeRule(
            # Normalize the rule's tool at load, exactly as the engine
            # normalizes the incoming action tool before matching. Otherwise
            # `tool: DB_DELETE` in the YAML never matches the normalized
            # `db_delete` and the red line silently never fires - the very
            # evasion normalize_tool exists to close, left open on the rule
            # side (white-box audit).
            name=rule["name"], tool=normalize_tool(rule["tool"]),
            window=rule["window"], metric=rule["metric"], max=rule["max"],
            route=rule["route"], reason=rule["reason"],
        ))
    return rules


def check(
    rules: list[CumulativeRule],
    tool: str,
    totals_lookup: Callable[[str, str], dict],
    pending_affected: int,
    *,
    included: bool = False,
) -> CumulativeVerdict | None:
    """First cumulative rule that fires for this tool, or None.

    `tool` must already be canonical (the caller runs normalize_tool, exactly
    as evaluate_action does before matching hard overrides - otherwise
    "DB_DELETE" would evade the budget the same way it once evaded overrides).

    `totals_lookup(window_kind, tool)` returns {"calls": int, "affected": int}
    for the CURRENT instance of that window (the caller resolves session_id /
    today's UTC bucket, so this module stays free of clocks and storage).

    CRITICAL SEMANTICS - the current proposal counts, once and exactly once.
    The rule fires when totals-INCLUDING-this-action EXCEED max: with
    max=100, the 101st single-record delete in a session fires; the 100th
    does not. Two call patterns keep that boundary:

      included=False - check-then-record caller (unit tests, external
                       integrations): the totals hold only the PAST, so this
                       action is added here (pending_affected for "affected",
                       exactly one for "calls").
      included=True  - record-first caller (the service): the caller wrote
                       this action into the ledger BEFORE checking, so its
                       weight is already inside the totals and nothing is
                       added. Recording first closes the TOCTOU where N
                       concurrent evaluations all read the same stale total
                       and all slipped under the budget together; adding a
                       pending contribution on top would double-count and
                       fire one action early.

    Fail closed: if the ledger cannot be read, or returns something that
    cannot be interpreted, the rule FIRES. An unreadable ledger means the
    gate cannot prove the budget is intact, and "cannot prove safe" routes to
    a human - same construction as _override_matches.
    """
    for rule in rules:
        if rule.tool != tool:
            continue
        try:
            totals = totals_lookup(rule.window, rule.tool)
            so_far = int(totals[rule.metric])
            if included:
                pending = 0   # already recorded: the totals ARE the verdict input
            else:
                # The current proposal contributes its blast radius to
                # "affected" and exactly one call to "calls". pending_affected
                # is clamped so a crafted negative count cannot buy back budget.
                pending = (max(0, int(pending_affected))
                           if rule.metric == "affected" else 1)
            fired = so_far + pending > rule.max
        except Exception:
            fired = True   # fail closed: unreadable ledger -> human review
        if fired:
            return CumulativeVerdict(rule.name, rule.route, rule.reason)
    return None


def velocity_check(max_per_minute: int, current: int, *,
                   included: bool = False) -> bool:
    """Does the CURRENT proposal breach the per-minute budget?

    Same boundary either way (max=60: the 60th proposal in a minute passes,
    the 61st fires); `included` says where the current proposal sits:

      included=False - `current` was read BEFORE this proposal was recorded,
                       so it is added here: current + 1 > max.
      included=True  - the caller recorded this proposal first (record-first
                       TOCTOU close, see service.run_evaluation), so it is
                       already inside `current`: current > max.

    Uninterpretable inputs fail closed: a throttle that shrugs off garbage
    is no throttle during the flood it exists for.
    """
    try:
        return int(current) + (0 if included else 1) > int(max_per_minute)
    except Exception:
        return True


# Suggested defaults. The orchestrator moves these into policy.yaml under a
# `cumulative_rules:` key (see docs/integration/cumulative.md) so operators
# tune budgets in the same file that holds every other red line.
DEFAULT_RULES: list[dict] = [
    {
        "name": "bulk_delete_per_session",
        "tool": "db_delete",
        "window": "session",
        "metric": "affected",
        "max": 100,
        "route": "REVIEW",
        "reason": "More than 100 records deleted across one session requires "
                  "human review (cumulative counterpart of "
                  "bulk_delete_always_review).",
    },
    {
        "name": "deletes_per_day",
        "tool": "db_delete",
        "window": "day",
        "metric": "calls",
        "max": 500,
        "route": "REVIEW",
        "reason": "More than 500 delete calls in one UTC day requires human "
                  "review.",
    },
]

# Velocity is not a CumulativeRule (it has no window/metric choice - it is a
# rate on proposals themselves), so it ships as its own trio of constants.
DEFAULT_MAX_PROPOSALS_PER_MINUTE = 60
VELOCITY_ROUTE = "CONFIRM"
VELOCITY_REASON = ("Proposal rate above the per-minute budget - every action "
                   "requires confirmation while the agent is hot (queue-flood "
                   "guard).")

"""Risk -> autonomy routing.

Order of authority (highest first):
  1. Hard overrides from policy.yaml  - deterministic rules, first match wins.
  2. Threshold mapping on the risk score.

This ordering is the core governance principle of AutonomyGate:
a probabilistic score can never overrule an explicit policy rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .scorer import Policy, RiskBreakdown, score_action


class Route(str, Enum):
    AUTONOMOUS = "AUTONOMOUS"   # execute immediately
    CONFIRM = "CONFIRM"         # show preview, wait for user confirmation
    REVIEW = "REVIEW"           # queue for human reviewer


@dataclass(frozen=True)
class Verdict:
    route: Route
    risk: RiskBreakdown
    matched_override: str | None  # name of the hard override, if one fired
    reason: str


def _override_matches(rule: dict, tool: str, params: dict, affected_count: int) -> bool:
    if rule.get("tool") != tool:
        return False
    if "min_affected_count" in rule and affected_count < rule["min_affected_count"]:
        return False
    if "param" in rule:
        value = str(params.get(rule["param"], ""))
        if "matches" in rule and not re.fullmatch(rule["matches"], value):
            return False
        if "not_matches" in rule and re.fullmatch(rule["not_matches"], value):
            return False
    return True


def evaluate_action(
    policy: Policy,
    tool: str,
    params: dict,
    affected_count: int = 1,
    model_confidence: float = 0.8,
    calibration_adjustment: float = 0.0,
) -> Verdict:
    risk = score_action(policy, tool, affected_count, model_confidence)

    # 1. Hard overrides bypass scoring entirely.
    for rule in policy.hard_overrides:
        if _override_matches(rule, tool, params, affected_count):
            return Verdict(
                route=Route(rule["route"]),
                risk=risk,
                matched_override=rule["name"],
                reason=rule["reason"],
            )

    # 2. Threshold mapping, with bounded calibration adjustment (bonus feature).
    #    Calibration is capped in calibration.py to +/-20 and can only shift
    #    scores, never bypass overrides above.
    adjusted = risk.total + calibration_adjustment
    if adjusted < policy.autonomous_below:
        return Verdict(Route.AUTONOMOUS, risk, None,
                       f"Risk {risk.total} (adj {adjusted:.1f}) below autonomous threshold "
                       f"{policy.autonomous_below}.")
    if adjusted > policy.review_above:
        return Verdict(Route.REVIEW, risk, None,
                       f"Risk {risk.total} (adj {adjusted:.1f}) above review threshold "
                       f"{policy.review_above}.")
    return Verdict(Route.CONFIRM, risk, None,
                   f"Risk {risk.total} (adj {adjusted:.1f}) in confirmation band.")

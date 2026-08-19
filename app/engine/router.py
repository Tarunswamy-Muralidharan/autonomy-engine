"""Risk -> autonomy routing.

Order of authority (highest first):
  1. Hard overrides from policy.yaml  - deterministic rules, first match wins.
  2. Threshold mapping on the risk score.

This ordering is the core governance principle of AutonomyGate:
a probabilistic score can never overrule an explicit policy rule.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from .calibration import MAX_ADJUSTMENT
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


_RECIPIENT_SPLIT = re.compile(r"[,;]")


def _override_matches(rule: dict, tool: str, params: dict, affected_count: int) -> bool:
    """Does this hard-override rule fire for this action?

    Fail-closed by construction: any missing parameter, unparseable value, or
    ambiguity makes the rule FIRE (routing to human review) rather than pass.
    """
    if rule.get("tool") != tool:
        return False
    if "min_affected_count" in rule and affected_count < rule["min_affected_count"]:
        return False

    if "param" in rule:
        if rule["param"] not in params:
            return True  # fail closed: the rule guards a param that wasn't supplied
        value = str(params[rule["param"]]).strip()
        if not value:
            return True

        # all_recipients_match: the rule fires unless EVERY comma/semicolon
        # separated entry matches (used for "is every recipient internal?").
        # A single external address in a list must not be masked by an
        # internal one - that was a real bypass found in adversarial testing.
        if "all_recipients_match" in rule:
            pattern = re.compile(rule["all_recipients_match"], re.IGNORECASE)
            entries = [e.strip() for e in _RECIPIENT_SPLIT.split(value)]
            return not all(e and pattern.fullmatch(e) for e in entries)

        flags = re.IGNORECASE if rule.get("ignore_case", True) else 0
        if "matches" in rule and not re.fullmatch(rule["matches"], value, flags):
            return False
        if "not_matches" in rule and re.fullmatch(rule["not_matches"], value, flags):
            return False
    return True


def normalize_tool(tool: str) -> str:
    """Canonical tool name for policy matching.

    Override rules are keyed on exact names; adversarial testing showed
    "DB_DELETE", " db_delete", "db_delete\\n" and unicode variants all evaded
    them. Normalizing before matching closes that class of evasion.
    """
    cleaned = unicodedata.normalize("NFKC", tool).strip().lower()
    return "".join(ch for ch in cleaned if ch.isprintable() and not ch.isspace())


def evaluate_action(
    policy: Policy,
    tool: str,
    params: dict,
    affected_count: int = 1,
    model_confidence: float = 0.8,
    calibration_adjustment: float = 0.0,
) -> Verdict:
    tool = normalize_tool(tool)
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

    # 2. Threshold mapping with bounded calibration.
    #
    #    Calibration is clamped here (defence in depth - compute_adjustment
    #    also caps it) and is deliberately ASYMMETRIC:
    #      - earned trust (negative) may open the AUTONOMOUS door, but may
    #        NEVER pull an action out of the human-REVIEW tier;
    #      - earned distrust (positive) may push an action INTO review.
    #    Adversarial testing showed a symmetric adjustment silently demoted
    #    100-record bulk deletes from REVIEW to one-click CONFIRM.
    adjustment = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, calibration_adjustment))
    adjusted = risk.total + adjustment
    review_score = max(risk.total, adjusted)   # trust cannot rescue a REVIEW

    if review_score > policy.review_above:
        return Verdict(Route.REVIEW, risk, None,
                       f"Risk {risk.total} (adj {adjusted:.1f}) above review threshold "
                       f"{policy.review_above}.")
    if adjusted < policy.autonomous_below:
        return Verdict(Route.AUTONOMOUS, risk, None,
                       f"Risk {risk.total} (adj {adjusted:.1f}) below autonomous threshold "
                       f"{policy.autonomous_below}.")
    return Verdict(Route.CONFIRM, risk, None,
                   f"Risk {risk.total} (adj {adjusted:.1f}) in confirmation band.")

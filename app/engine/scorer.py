"""Risk scorer: turns one proposed agent action into a 0-100 risk score.

Four dimensions, each 0-100, combined as a weighted sum:
  reversibility  - can the action be undone?          (policy lookup per tool)
  data_scope     - how many records/users affected?   (log-scale on count)
  regulatory     - is the data regulated (PII etc.)?  (policy lookup per tool)
  confidence     - how sure is the model of itself?   ((1 - confidence) * 100)

Deliberately NOT an LLM call: risk scoring must be deterministic,
explainable, testable, and free. The model contributes exactly one
bounded input (its self-reported confidence).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml

POLICY_PATH = Path(__file__).parent / "policy.yaml"


@dataclass(frozen=True)
class RiskBreakdown:
    reversibility: float
    data_scope: float
    regulatory: float
    confidence: float
    total: float

    def as_dict(self) -> dict:
        return {k: round(v, 1) for k, v in asdict(self).items()}


class Policy:
    """Loaded snapshot of policy.yaml. Reload by constructing a new instance."""

    def __init__(self, path: Path = POLICY_PATH):
        with open(path, encoding="utf-8") as f:
            self._raw = yaml.safe_load(f)
        self.weights: dict[str, float] = self._raw["weights"]
        self.autonomous_below: float = self._raw["thresholds"]["autonomous_below"]
        self.review_above: float = self._raw["thresholds"]["review_above"]
        self.hard_overrides: list[dict] = self._raw.get("hard_overrides", [])
        # Cumulative rules are red lines too: a malformed one that silently
        # never fires is a fail-open hole, so parse_rules raises at load and
        # the service refuses to start - same discipline as _validate.
        from ..cumulative.rules import (DEFAULT_MAX_PROPOSALS_PER_MINUTE,
                                        VELOCITY_REASON, parse_rules)
        self.cumulative_rules = parse_rules(self._raw.get("cumulative_rules", []))
        _velocity = self._raw.get("velocity", {})
        self.max_proposals_per_minute = int(
            _velocity.get("max_proposals_per_minute",
                          DEFAULT_MAX_PROPOSALS_PER_MINUTE))
        self.velocity_reason = _velocity.get("reason", VELOCITY_REASON)
        self._reversibility: dict[str, float] = self._raw["reversibility"]
        self._regulatory: dict[str, float] = self._raw["regulatory"]
        self._validate()

    def _validate(self) -> None:
        """Reject a malformed policy at load rather than at request time.

        A typo in a rule's `tool` or a bad regex used to mean the rule
        silently never fired - a fail-OPEN hole in the red lines. The service
        now refuses to start on an invalid policy.
        """
        import re as _re

        required = {"name", "tool", "route"}
        seen: set[str] = set()
        for i, rule in enumerate(self.hard_overrides):
            missing = required - rule.keys()
            if missing:
                raise ValueError(f"hard_overrides[{i}] missing {sorted(missing)}")
            if rule["name"] in seen:
                raise ValueError(f"duplicate override name: {rule['name']}")
            seen.add(rule["name"])
            if rule["route"] not in ("AUTONOMOUS", "CONFIRM", "REVIEW"):
                raise ValueError(f"{rule['name']}: invalid route {rule['route']!r}")
            for key in ("matches", "not_matches", "all_recipients_match"):
                if key in rule:
                    try:
                        _re.compile(rule[key])
                    except _re.error as exc:
                        raise ValueError(f"{rule['name']}.{key}: bad regex ({exc})")
            has_matcher = any(k in rule for k in
                              ("matches", "not_matches", "all_recipients_match"))
            if "param" in rule and not has_matcher:
                raise ValueError(f"{rule['name']}: 'param' without a matcher would "
                                 f"fire on every call")
            if has_matcher and "param" not in rule:
                raise ValueError(f"{rule['name']}: matcher without a 'param' to "
                                 f"apply it to")
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError(f"risk weights must sum to 1.0, got "
                             f"{sum(self.weights.values())}")
        if not 0 <= self.autonomous_below <= self.review_above <= 100:
            raise ValueError("thresholds must satisfy 0 <= autonomous <= review <= 100")

    def reversibility_for(self, tool: str) -> float:
        return float(self._reversibility.get(tool, self._reversibility["default"]))

    def regulatory_for(self, tool: str) -> float:
        return float(self._regulatory.get(tool, self._regulatory["default"]))


def data_scope_score(affected_count: int) -> float:
    """1 record -> 10, 100 -> 60, ~4000 -> 100. Smooth log scale."""
    n = max(1, int(affected_count))
    return min(100.0, 10.0 + 25.0 * math.log10(n))


# The model reports its own confidence, so it must never be able to zero out
# this dimension and single-handedly move an action into a lower tier.
# Clamping the CEILING guarantees the confidence dimension always contributes
# at least (1 - MAX_TRUSTED_CONFIDENCE) * 100 * weight to the risk score.
MAX_TRUSTED_CONFIDENCE = 0.9


def score_action(
    policy: Policy,
    tool: str,
    affected_count: int = 1,
    model_confidence: float = 0.8,
) -> RiskBreakdown:
    model_confidence = min(MAX_TRUSTED_CONFIDENCE, max(0.0, model_confidence))
    rev = policy.reversibility_for(tool)
    scope = data_scope_score(affected_count)
    reg = policy.regulatory_for(tool)
    conf = (1.0 - model_confidence) * 100.0

    w = policy.weights
    total = (
        rev * w["reversibility"]
        + scope * w["data_scope"]
        + reg * w["regulatory"]
        + conf * w["confidence"]
    )
    return RiskBreakdown(rev, scope, reg, conf, round(total, 1))

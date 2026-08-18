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
        self._reversibility: dict[str, float] = self._raw["reversibility"]
        self._regulatory: dict[str, float] = self._raw["regulatory"]

    def reversibility_for(self, tool: str) -> float:
        return float(self._reversibility.get(tool, self._reversibility["default"]))

    def regulatory_for(self, tool: str) -> float:
        return float(self._regulatory.get(tool, self._regulatory["default"]))


def data_scope_score(affected_count: int) -> float:
    """1 record -> 10, 100 -> 60, ~4000 -> 100. Smooth log scale."""
    n = max(1, int(affected_count))
    return min(100.0, 10.0 + 25.0 * math.log10(n))


def score_action(
    policy: Policy,
    tool: str,
    affected_count: int = 1,
    model_confidence: float = 0.8,
) -> RiskBreakdown:
    model_confidence = min(1.0, max(0.0, model_confidence))
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

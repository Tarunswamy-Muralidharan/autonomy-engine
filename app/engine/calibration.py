"""Adaptive threshold calibration (bonus feature).

If humans consistently approve a given action type at the CONFIRM tier,
its risk drifts DOWN (toward autonomous). If they consistently reject it,
risk drifts UP (toward review).

Deliberately simple, explainable rules — not ML:
  - needs at least MIN_SAMPLES human decisions before any adjustment
  - >= 90% approvals  -> adjustment -10
  - >= 40% rejections -> adjustment +15
  - hard cap at +/- MAX_ADJUSTMENT so calibration can never overpower
    thresholds by much, and NEVER bypasses hard overrides (those are
    evaluated before scoring in router.py).

Every adjustment is derived from counts stored per action type, so the
current adjustment is always reproducible from the audit trail.
"""
from __future__ import annotations

MIN_SAMPLES = 10
MAX_ADJUSTMENT = 20.0


def compute_adjustment(stats: dict) -> float:
    approvals = max(0, stats.get("approvals", 0))   # approved WITHOUT modification
    rejections = max(0, stats.get("rejections", 0))
    modifications = max(0, stats.get("modifications", 0))
    # Derive the denominator from the outcomes themselves rather than trusting
    # a separate counter - a skewed `confirms` value could otherwise produce
    # ratios above 1.0 and silently unlock the trusted branch.
    decisions = approvals + rejections + modifications
    if decisions < MIN_SAMPLES:
        return 0.0
    adjustment = 0.0
    if approvals / decisions >= 0.9:
        adjustment = -10.0                          # trusted -> toward autonomous
    elif (rejections + modifications) / decisions >= 0.4:
        adjustment = 15.0                           # distrusted -> toward review
    return max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, adjustment))

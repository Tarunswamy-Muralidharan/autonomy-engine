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
    confirms = stats.get("confirms", 0)
    if confirms < MIN_SAMPLES:
        return 0.0
    approvals = stats.get("approvals", 0)
    rejections = stats.get("rejections", 0)
    adjustment = 0.0
    if approvals / confirms >= 0.9:
        adjustment = -10.0
    elif rejections / confirms >= 0.4:
        adjustment = 15.0
    return max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, adjustment))

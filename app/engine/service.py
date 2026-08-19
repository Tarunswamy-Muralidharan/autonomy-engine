"""Engine service: the single evaluation pipeline used by BOTH the public
/evaluate endpoint and the in-process sample agent. One code path, one
audit trail, regardless of who asks."""
from __future__ import annotations

import logging
import math
import time
import uuid

from pydantic import BaseModel, Field, field_validator

from ..audit.redact import redact_params
from ..storage.repo import get_repo
from .calibration import compute_adjustment
from .router import Route, evaluate_action
from .scorer import Policy

log = logging.getLogger("autonomygate.engine")


MAX_PARAM_DEPTH = 25          # DynamoDB rejects >32 levels; stay clear of it
MAX_AFFECTED_COUNT = 10 ** 12  # DynamoDB numbers cap at 38 significant digits


def param_depth(value, _depth: int = 0) -> int:
    if _depth > MAX_PARAM_DEPTH:
        return _depth
    if isinstance(value, dict):
        return max((param_depth(v, _depth + 1) for v in value.values()),
                   default=_depth)
    if isinstance(value, (list, tuple)):
        return max((param_depth(v, _depth + 1) for v in value), default=_depth)
    return _depth


class EvaluateRequest(BaseModel):
    """Validated at the edge so nothing unvalidated reaches storage.

    A 500 from this endpoint would mean an action was never scored, never
    routed and never audited - i.e. the gate fails OPEN. Every constraint
    below turns a former crash into an explicit 422.
    """
    model_config = {"extra": "ignore"}

    agent_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    tool: str = Field(min_length=1, max_length=256)
    params: dict = Field(default_factory=dict)
    affected_count: int = Field(1, ge=0, le=MAX_AFFECTED_COUNT)
    model_confidence: float = Field(0.8, ge=0.0, le=1.0, allow_inf_nan=False)
    preview: str = Field("", max_length=4096)

    @field_validator("params")
    @classmethod
    def _bounded_params(cls, v: dict) -> dict:
        if param_depth(v) > MAX_PARAM_DEPTH:
            raise ValueError(f"params nested deeper than {MAX_PARAM_DEPTH} levels")

        # Non-finite floats survive Python's json parser but DynamoDB rejects
        # them, which would turn an unscored action into a 500 (fail-open).
        def _finite(node):
            if isinstance(node, float) and not math.isfinite(node):
                raise ValueError("params must not contain NaN or Infinity")
            if isinstance(node, dict):
                for k, val in node.items():
                    _finite(k)
                    _finite(val)
            elif isinstance(node, (list, tuple)):
                for val in node:
                    _finite(val)
        _finite(v)
        return v

    @field_validator("agent_id", "session_id", "tool")
    @classmethod
    def _no_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


class EvaluateResponse(BaseModel):
    action_id: str
    route: Route
    risk: dict
    matched_override: str | None
    reason: str
    ticket_id: str | None = None


def observed_blast_radius(params: dict) -> int:
    """Largest collection found in the parameters.

    `affected_count` is SELF-REPORTED by the calling agent. Adversarial
    testing showed an agent could declare affected_count=1 while passing
    5000 record ids, dodging the bulk-delete override and deceiving the
    human reviewer. The engine therefore never trusts the declaration
    alone - it derives what it can from the payload itself.
    """
    largest = 0

    def _walk(value):
        nonlocal largest
        if isinstance(value, (list, tuple, set)):
            largest = max(largest, len(value))
            for v in value:
                _walk(v)
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)

    _walk(params)
    return largest


def run_evaluation(policy: Policy, req: EvaluateRequest) -> EvaluateResponse:
    repo = get_repo()
    stats = repo.get_calibration(req.tool)
    adjustment = compute_adjustment(stats)

    # Governance uses the WORST of declared vs observed scope: an agent may
    # under-report, never under-govern.
    declared = max(0, req.affected_count)
    effective_count = max(declared, observed_blast_radius(req.params))
    understated = effective_count > declared

    verdict = evaluate_action(
        policy,
        tool=req.tool,
        params=req.params,
        affected_count=effective_count,
        model_confidence=req.model_confidence,
        calibration_adjustment=adjustment,
    )

    action_id = str(uuid.uuid4())
    ticket_id: str | None = None

    if verdict.route in (Route.CONFIRM, Route.REVIEW):
        ticket_id = str(uuid.uuid4())
        repo.put_ticket({
            "ticket_id": ticket_id,
            "kind": "confirm" if verdict.route is Route.CONFIRM else "review",
            "ts": time.time(),
            "action_id": action_id,
            "agent_id": req.agent_id,
            "session_id": req.session_id,
            "tool": req.tool,
            "params": req.params,                 # kept so approval can execute the action
            "affected_count": req.affected_count,
            "risk_total": verdict.risk.total,
            # Bounded, redacted preview: this is display-only, and an
            # unbounded copy of params pushed the item toward DynamoDB's
            # 400KB ceiling while duplicating PII.
            "preview": (req.preview
                        or f"{req.tool}({redact_params(req.params)})")[:2000],
        })

    repo.put_audit({
        "action_id": action_id,
        "session_id": req.session_id,
        "agent_id": req.agent_id,
        "ts": time.time(),
        "tool": req.tool,
        "params_redacted": redact_params(req.params),
        "affected_count": req.affected_count,
        "risk_breakdown": verdict.risk.as_dict(),
        "calibration_adjustment": adjustment,
        "route": verdict.route.value,
        "matched_override": verdict.matched_override,
        "reason": verdict.reason,
        "ticket_id": ticket_id,
    })
    log.info('{"event": "evaluated", "action_id": "%s", "tool": "%s", '
             '"route": "%s", "risk": %s, "override": "%s"}',
             action_id, req.tool, verdict.route.value,
             verdict.risk.total, verdict.matched_override)

    return EvaluateResponse(
        action_id=action_id,
        route=verdict.route,
        risk=verdict.risk.as_dict(),
        matched_override=verdict.matched_override,
        reason=verdict.reason,
        ticket_id=ticket_id,
    )

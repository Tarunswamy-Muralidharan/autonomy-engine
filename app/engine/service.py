"""Engine service: the single evaluation pipeline used by BOTH the public
/evaluate endpoint and the in-process sample agent. One code path, one
audit trail, regardless of who asks."""
from __future__ import annotations

import logging
import time
import uuid

from pydantic import BaseModel, Field

from ..audit.redact import redact_params
from ..storage.repo import get_repo
from .calibration import compute_adjustment
from .router import Route, evaluate_action
from .scorer import Policy

log = logging.getLogger("autonomygate.engine")


class EvaluateRequest(BaseModel):
    agent_id: str
    session_id: str
    tool: str
    params: dict = Field(default_factory=dict)
    affected_count: int = 1
    model_confidence: float = Field(0.8, ge=0.0, le=1.0)
    preview: str = ""


class EvaluateResponse(BaseModel):
    action_id: str
    route: Route
    risk: dict
    matched_override: str | None
    reason: str
    ticket_id: str | None = None


def run_evaluation(policy: Policy, req: EvaluateRequest) -> EvaluateResponse:
    repo = get_repo()
    stats = repo.get_calibration(req.tool)
    adjustment = compute_adjustment(stats)

    verdict = evaluate_action(
        policy,
        tool=req.tool,
        params=req.params,
        affected_count=req.affected_count,
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
            "preview": req.preview or f"{req.tool}({req.params})",
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

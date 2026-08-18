"""AutonomyGate — Graduated Autonomy Engine (Aivar PS-9.1).

FastAPI service that risk-scores agent actions and routes them to
AUTONOMOUS / CONFIRM / REVIEW, with a persisted audit trail.
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audit.redact import redact_params
from .engine.calibration import compute_adjustment
from .engine.router import Route, evaluate_action
from .engine.scorer import Policy
from .storage.repo import get_repo

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "msg": %(message)s}',
)
log = logging.getLogger("autonomygate")

APP_VERSION = "1.0.0"

app = FastAPI(
    title="AutonomyGate",
    description="Graduated Autonomy Engine: risk-scores every AI-agent action and "
                "routes it to autonomous execution, user confirmation, or human review.",
    version=APP_VERSION,
)

policy = Policy()


# ---------- models ----------

class EvaluateRequest(BaseModel):
    agent_id: str
    session_id: str
    tool: str
    params: dict = Field(default_factory=dict)
    affected_count: int = 1
    model_confidence: float = Field(0.8, ge=0.0, le=1.0)
    preview: str = ""          # human-readable "what is about to happen"


class EvaluateResponse(BaseModel):
    action_id: str
    route: Route
    risk: dict
    matched_override: str | None
    reason: str
    ticket_id: str | None = None


class Decision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    decided_by: str = "user"
    note: str = ""


class OutcomeUpdate(BaseModel):
    outcome: str = Field(pattern="^(executed|failed|skipped)$")
    decided_by: str = "system"


# ---------- core endpoint ----------

@app.post("/evaluate", response_model=EvaluateResponse)
def evaluate(req: EvaluateRequest) -> EvaluateResponse:
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
            "risk_total": verdict.risk.total,
            "preview": req.preview or f"{req.tool}({req.params})",
        })

    audit_record = {
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
    }
    repo.put_audit(audit_record)
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


# ---------- confirmation & review ----------

@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str) -> dict:
    ticket = get_repo().get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "ticket not found")
    return ticket


@app.post("/tickets/{ticket_id}/decision")
def decide_ticket(ticket_id: str, body: Decision) -> dict:
    repo = get_repo()
    ticket = repo.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "ticket not found")
    decision = "approved" if body.decision == "approve" else "rejected"
    updated = repo.decide_ticket(ticket_id, decision, body.decided_by, body.note)
    if not updated:
        raise HTTPException(409, "ticket already decided")

    # Calibration learns from CONFIRM-tier human decisions (bonus feature).
    if updated["kind"] == "confirm":
        repo.update_calibration(updated["tool"], decision)

    outcome = "approved" if decision == "approved" else "denied"
    repo.update_audit_outcome(updated["action_id"], outcome, body.decided_by)
    log.info('{"event": "ticket_decided", "ticket_id": "%s", "decision": "%s", '
             '"by": "%s"}', ticket_id, decision, body.decided_by)
    return updated


@app.get("/queue")
def queue(status: str = "pending", kind: str | None = None) -> list[dict]:
    return get_repo().list_tickets(status=status, kind=kind)


# ---------- audit ----------

@app.get("/audit")
def audit(session_id: str | None = None, agent_id: str | None = None,
          limit: int = 100) -> list[dict]:
    return get_repo().query_audit(session_id=session_id, agent_id=agent_id, limit=limit)


@app.post("/audit/{action_id}/outcome")
def report_outcome(action_id: str, body: OutcomeUpdate) -> dict:
    ok = get_repo().update_audit_outcome(action_id, body.outcome, body.decided_by)
    if not ok:
        raise HTTPException(404, "action not found")
    return {"action_id": action_id, "outcome": body.outcome}


# ---------- calibration inspection ----------

@app.get("/calibration/{action_type}")
def calibration(action_type: str) -> dict:
    stats = get_repo().get_calibration(action_type)
    stats["current_adjustment"] = compute_adjustment(stats)
    return stats


# ---------- health ----------

@app.get("/health")
def health() -> dict:
    checks = {"storage": "ok"}
    try:
        get_repo().list_tickets(status="pending")
    except Exception as exc:  # pragma: no cover
        checks["storage"] = f"error: {exc}"
    status = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "version": APP_VERSION, "checks": checks}


# ---------- error handling ----------

@app.exception_handler(Exception)
async def unhandled(request, exc):  # pragma: no cover
    log.error('{"event": "unhandled_error", "path": "%s", "error": "%s"}',
              request.url.path, exc)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"detail": "internal error"})

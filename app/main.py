"""AutonomyGate — Graduated Autonomy Engine (Aivar PS-9.1).

FastAPI service that risk-scores agent actions and routes them to
AUTONOMOUS / CONFIRM / REVIEW, with a persisted audit trail, a governed
sample agent (Bedrock Claude or scripted), and a minimal dashboard.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent import tools as agent_tools
from .agent.support_agent import run_task
from .engine.calibration import compute_adjustment
from .engine.scorer import Policy
from .engine.service import EvaluateRequest, EvaluateResponse, run_evaluation
from .storage.repo import get_repo

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "msg": %(message)s}',
)
log = logging.getLogger("autonomygate")

APP_VERSION = "1.0.0"
STATIC_DIR = Path(__file__).parent.parent / "static"

app = FastAPI(
    title="AutonomyGate",
    description="Graduated Autonomy Engine: risk-scores every AI-agent action and "
                "routes it to autonomous execution, user confirmation, or human review.",
    version=APP_VERSION,
)

policy = Policy()


class Decision(BaseModel):
    decision: str = Field(pattern="^(approve|reject|modify)$")
    decided_by: str = "user"
    note: str = ""
    edited_params: dict | None = None   # required when decision == "modify"


class OutcomeUpdate(BaseModel):
    outcome: str = Field(pattern="^(executed|failed|skipped)$")
    decided_by: str = "system"


class AgentTask(BaseModel):
    task: str
    session_id: str | None = None


# ---------- core evaluation API (for ANY external agent) ----------

@app.post("/evaluate", response_model=EvaluateResponse)
def evaluate(req: EvaluateRequest) -> EvaluateResponse:
    return run_evaluation(policy, req)


# ---------- the governed sample agent ----------

@app.post("/agent/task")
def agent_task(body: AgentTask) -> dict:
    try:
        return run_task(policy, body.task, body.session_id)
    except Exception as exc:
        log.error('{"event": "agent_error", "error": "%s"}', exc)
        raise HTTPException(502, f"agent failure: {exc}") from exc


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
    decision = {"approve": "approved", "reject": "rejected",
                "modify": "modified"}[body.decision]
    if decision == "modified" and not body.edited_params:
        raise HTTPException(422, "decision 'modify' requires edited_params")
    updated = repo.decide_ticket(ticket_id, decision, body.decided_by, body.note)
    if not updated:
        raise HTTPException(409, "ticket already decided")

    # Calibration learns from CONFIRM-tier human decisions (bonus feature):
    # clean approvals lower future risk; rejections AND modifications raise it.
    if updated["kind"] == "confirm":
        repo.update_calibration(updated["tool"], decision)

    execution_result = None
    if decision in ("approved", "modified"):
        # Human said yes -> the action executes. On "modify", the human's
        # edited parameters REPLACE the agent's (human-authored = authorized).
        exec_params = (body.edited_params if decision == "modified"
                       else updated.get("params", {}))
        try:
            execution_result, _ = agent_tools.execute_tool(updated["tool"], exec_params)
            outcome = "executed" if decision == "approved" else "executed_modified"
            repo.update_audit_outcome(updated["action_id"], outcome, body.decided_by)
        except Exception as exc:
            repo.update_audit_outcome(updated["action_id"], "failed", "system")
            log.error('{"event": "approved_execution_failed", "ticket": "%s", '
                      '"error": "%s"}', ticket_id, exc)
            raise HTTPException(500, f"approved but execution failed: {exc}") from exc
    else:
        repo.update_audit_outcome(updated["action_id"], "denied", body.decided_by)

    log.info('{"event": "ticket_decided", "ticket_id": "%s", "decision": "%s", '
             '"by": "%s"}', ticket_id, decision, body.decided_by)
    updated["execution_result"] = execution_result
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


# ---------- dashboard & health ----------

@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    checks = {"storage": "ok"}
    try:
        get_repo().list_tickets(status="pending")
    except Exception as exc:  # pragma: no cover
        checks["storage"] = f"error: {exc}"
    import os
    checks["agent_mode"] = os.environ.get("AUTONOMYGATE_AGENT", "scripted")
    status = "healthy" if checks["storage"] == "ok" else "degraded"
    return {"status": status, "version": APP_VERSION, "checks": checks}


@app.exception_handler(Exception)
async def unhandled(request, exc):  # pragma: no cover
    log.error('{"event": "unhandled_error", "path": "%s", "error": "%s"}',
              request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})

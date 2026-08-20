"""AutonomyGate — Graduated Autonomy Engine (Aivar PS-9.1).

FastAPI service that risk-scores agent actions and routes them to
AUTONOMOUS / CONFIRM / REVIEW, with a persisted audit trail, a governed
sample agent (Bedrock Claude or scripted), and a minimal dashboard.
"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent import tools as agent_tools
from .agent.support_agent import run_task
from .engine.calibration import compute_adjustment
from .engine.scorer import Policy
from .engine.router import Route, evaluate_action
from .engine.service import (EvaluateRequest, EvaluateResponse,
                             observed_blast_radius, run_evaluation)
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

REVIEWER_TOKEN = os.environ.get("AUTONOMYGATE_REVIEWER_TOKEN", "")


def is_reviewer(authorization: str = Header(default="")) -> bool:
    """Optional-auth variant of require_reviewer for endpoints that serve a
    REDUCED view to unauthenticated callers instead of a 401.

    Exists because of a disclosure chain found in external review: GET /audit
    (then open) leaked ticket_ids, and GET /tickets/{id} (then open) returned
    the ticket's RAW params - so an unauthenticated stranger could walk the
    audit log into the one store that deliberately holds unredacted PII,
    bypassing the /queue lock entirely.
    """
    if not REVIEWER_TOKEN:
        return True  # unset (local dev): open, matching require_reviewer
    scheme, _, token = authorization.partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(token, REVIEWER_TOKEN)


def require_reviewer(authorization: str = Header(default="")) -> str:
    """Auth for the REVIEW PLANE (queue, decisions, outcome reporting).

    The proposal plane (/evaluate) is deliberately open - it is the
    integration surface any agent calls. The review plane is not: adversarial
    testing showed that without this, a caller could propose a high-risk
    action and then approve its own ticket, which makes REVIEW/CONFIRM
    decorative. In production this becomes an API Gateway JWT authorizer +
    Cognito reviewer identities; the bearer token is the minimum viable
    control that closes the self-approval hole today.
    """
    if not REVIEWER_TOKEN:
        return "anonymous-reviewer"  # unset (local dev): open, as before
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, REVIEWER_TOKEN):
        raise HTTPException(401, "reviewer credentials required")
    return "authenticated-reviewer"


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
    import json as _json
    if len(_json.dumps(req.params, default=str)) > 200_000:
        raise HTTPException(413, "params too large (200KB limit); pass a reference "
                                 "(e.g. an object key) instead of inline payloads")
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

# The fields an UNAUTHENTICATED caller may see on a ticket. This is exactly
# what the external-integration contract needs (poll status, then execute):
# no params, no preview - the raw params are the most sensitive data in the
# system and are reserved for the authenticated review plane.
_PUBLIC_TICKET_FIELDS = ("ticket_id", "status", "kind", "ts", "action_id",
                         "decided_at", "risk_total")


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, reviewer: bool = Depends(is_reviewer)) -> dict:
    ticket = get_repo().get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "ticket not found")
    if reviewer:
        return ticket
    return {k: ticket[k] for k in _PUBLIC_TICKET_FIELDS if k in ticket}


@app.post("/tickets/{ticket_id}/decision")
def decide_ticket(ticket_id: str, body: Decision,
                  reviewer: str = Depends(require_reviewer)) -> dict:
    repo = get_repo()
    ticket = repo.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "ticket not found")
    decision = {"approve": "approved", "reject": "rejected",
                "modify": "modified"}[body.decision]

    # Separation of duties: the agent that proposed an action may never be the
    # principal that approves it (found in adversarial testing - a malicious
    # agent self-approved its own exfiltration ticket).
    if decision != "rejected" and body.decided_by == ticket.get("agent_id"):
        raise HTTPException(403, "separation of duties: the proposing agent "
                                 "cannot approve its own action")

    if decision == "modified":
        if not body.edited_params:
            raise HTTPException(422, "decision 'modify' requires edited_params")
        # Edited parameters are a NEW proposal and are re-governed from
        # scratch. Without this, a reviewer could turn an approved 3-record
        # delete into a 5000-record delete and skip the hard overrides
        # entirely - proven by adversarial testing.
        # Side-effect-free re-governance of the edited parameters (no new
        # ticket, no audit row - this is an authority check, not a proposal).
        recheck = evaluate_action(
            policy, tool=ticket["tool"], params=body.edited_params,
            affected_count=max(1, observed_blast_radius(body.edited_params)),
            model_confidence=0.5, calibration_adjustment=0.0)
        tier = {Route.AUTONOMOUS: 0, Route.CONFIRM: 1, Route.REVIEW: 2}
        ticket_tier = 2 if ticket.get("kind") == "review" else 1
        if tier[recheck.route] > ticket_tier:
            # The edit ESCALATES beyond the authority of this ticket (e.g. a
            # 3-record delete edited into a 5000-record delete). Refuse to
            # execute; the reviewer must raise a fresh, higher-tier proposal.
            raise HTTPException(409, {
                "error": "edited parameters escalate risk beyond this ticket's tier",
                "required_route": recheck.route.value,
                "matched_override": recheck.matched_override,
                "reason": recheck.reason,
            })
    updated = repo.decide_ticket(ticket_id, decision, body.decided_by, body.note)
    if not updated:
        raise HTTPException(409, "ticket already decided")

    # Calibration learns from CONFIRM-tier human decisions (bonus feature):
    # clean approvals lower future risk; rejections AND modifications raise it.
    if updated["kind"] == "confirm":
        repo.update_calibration(updated["tool"], decision)

    execution_result = None
    if decision in ("approved", "modified"):
        if not agent_tools.is_local_tool(updated["tool"]):
            # External integration contract: the engine GOVERNS the action but
            # the calling stack owns the tool. Approval is recorded; the caller
            # polls the ticket, executes, and reports via /audit/{id}/outcome.
            repo.update_audit_outcome(updated["action_id"],
                                      "approved_pending_external_execution",
                                      body.decided_by)
            updated["execution_result"] = None
            updated["note"] = (updated.get("note") or "") + \
                " [external tool: execute in your stack, then report the outcome]"
            return updated
        # Local tool -> the action executes here. On "modify", the human's
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
def queue(status: str = "pending", kind: str | None = None,
          reviewer: str = Depends(require_reviewer)) -> list[dict]:
    return get_repo().list_tickets(status=status or "pending", kind=kind)


# ---------- audit ----------

@app.get("/audit")
def audit(session_id: str | None = None, agent_id: str | None = None,
          limit: int = 100, reviewer: str = Depends(require_reviewer)) -> list[dict]:
    # Reviewer-only: audit rows are redacted, but they still expose ticket_ids
    # (a pivot into the raw-params store), agent identities, tool usage and
    # decision patterns - operational intelligence an anonymous caller has no
    # business reading on a governance product.
    limit = max(1, min(limit, 1000))
    return get_repo().query_audit(session_id=session_id, agent_id=agent_id, limit=limit)


_TERMINAL_OUTCOMES = {"executed", "executed_modified", "denied", "failed", "skipped"}


@app.post("/audit/{action_id}/outcome")
def report_outcome(action_id: str, body: OutcomeUpdate,
                   reviewer: str = Depends(require_reviewer)) -> dict:
    # The audit trail is append-only in spirit: an outcome may be written once,
    # from the pending state. Adversarial testing showed an attacker could
    # otherwise rewrite history (mark a denied action "executed", or a real
    # execution "failed") to cover tracks.
    repo = get_repo()
    record = repo.get_audit(action_id)          # keyed lookup, never a scan
    if record is None:
        raise HTTPException(404, "action not found")
    current = record.get("final_outcome", "pending")
    if current in _TERMINAL_OUTCOMES:
        raise HTTPException(409, f"outcome already recorded as '{current}'; "
                                 "the audit trail is append-only")
    # The storage layer re-checks the pending precondition atomically, so
    # concurrent reporters across Lambda instances cannot both win.
    if not repo.update_audit_outcome(action_id, body.outcome, body.decided_by):
        raise HTTPException(409, "outcome already recorded")
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


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc: RequestValidationError):
    """Render validation errors safely.

    Pydantic echoes the offending input back in the error payload - and a
    non-finite float (NaN/Infinity) is not JSON-serializable, so rendering
    the 422 itself used to crash into a 500. A malformed request must never
    become a server error on a governance gate.
    """
    def _safe(node):
        if isinstance(node, float) and not math.isfinite(node):
            return str(node)
        if isinstance(node, dict):
            return {k: _safe(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [_safe(v) for v in node]
        if isinstance(node, (str, int, bool, type(None))):
            return node
        return str(node)

    return JSONResponse(status_code=422, content={"detail": _safe(exc.errors())})


@app.exception_handler(Exception)
async def unhandled(request, exc):  # pragma: no cover
    log.error('{"event": "unhandled_error", "path": "%s", "error": "%s"}',
              request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})

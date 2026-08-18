"""The governed sample agent.

Two interchangeable planners (env AUTONOMYGATE_AGENT=scripted|bedrock):

  scripted  - deterministic keyword planner. Zero cloud dependency; used by
              tests and offline dev. Proves the GOVERNANCE path regardless
              of any LLM's mood.
  bedrock   - Amazon Bedrock (Claude) tool-calling loop via the Converse
              API. The production demo mode. Each tool schema carries a
              required `confidence` field the model must fill in — that
              self-assessment feeds the risk scorer's confidence dimension.

Both planners emit the same thing: a sequence of proposed tool calls.
EVERY proposed call goes through AutonomyGate BEFORE execution:

  proposal -> /evaluate -> AUTONOMOUS: execute now
                        -> CONFIRM/REVIEW: park on the ticket; the tool runs
                           only if a human approves it (see main.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid

from ..engine.router import Route
from ..engine.scorer import Policy
from ..engine.service import EvaluateRequest, run_evaluation
from . import tools as T

log = logging.getLogger("autonomygate.agent")

AGENT_ID = "support-ops-agent"

SYSTEM_PROMPT = (
    "You are a customer-support operations agent for OurCorp. You act on the "
    "CRM through the provided tools. Rules: use the minimal tool that solves "
    "the task; report exactly what you did or why an action is pending "
    "governance approval; never retry an action that was denied. With every "
    "tool call, set the `confidence` field honestly (how sure you are that "
    "this exact call, with these exact parameters, is what the task needs)."
)


# ---------------------------------------------------------------- planners

def _scripted_plan(task: str) -> list[dict]:
    """Deterministic keyword planner: task text -> proposed tool calls."""
    t = task.lower()
    m_read = re.search(r"(?:customer|record)\s+#?(\d+)", t)
    if any(k in t for k in ("delete", "clean up", "cleanup", "remove")) and "inactive" in t:
        hits, _ = T.get_crm().crm_search(inactive_only=True)
        ids = [c["customer_id"] for c in hits]
        return [{"tool": "db_delete", "params": {"record_ids": ids}, "confidence": 0.75}]
    if "export" in t:
        scope = "all" if ("all" in t or "every" in t) else "summary"
        return [{"tool": "export_report", "params": {"scope": scope}, "confidence": 0.8}]
    if "email" in t:
        m_to = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", task)
        to = m_to.group(0) if m_to else "teammate@ourcorp.com"
        return [{"tool": "send_email",
                 "params": {"to": to, "body": f"Regarding: {task}"}, "confidence": 0.7}]
    m_upd = re.search(r"(?:update|change|set)\s+customer\s+#?(\d+)(?:'s)?\s+(\w+)\s+to\s+(\S+)", t)
    if m_upd:
        return [{"tool": "crm_update",
                 "params": {"customer_id": m_upd.group(1), "field": m_upd.group(2),
                            "value": m_upd.group(3)}, "confidence": 0.85}]
    if m_read:
        return [{"tool": "crm_read",
                 "params": {"customer_id": m_read.group(1)}, "confidence": 0.9}]
    return [{"tool": "crm_search", "params": {"query": ""}, "confidence": 0.6}]


def _bedrock_tool_config() -> dict:
    specs = []
    for spec in T.TOOL_SPECS:
        schema = json.loads(json.dumps(spec["schema"]))  # deep copy
        schema["properties"]["confidence"] = {
            "type": "number",
            "description": "Your honest confidence (0-1) that this exact call "
                           "with these parameters is correct for the task.",
        }
        schema["required"] = list(schema.get("required", [])) + ["confidence"]
        specs.append({"toolSpec": {"name": spec["name"],
                                   "description": spec["description"],
                                   "inputSchema": {"json": schema}}})
    return {"tools": specs}


class BedrockPlanner:
    """Claude tool-calling loop on Amazon Bedrock (Converse API)."""

    def __init__(self):
        import boto3
        region = os.environ.get("AWS_REGION", "ap-south-1")
        self.model_id = os.environ.get(
            "AUTONOMYGATE_BEDROCK_MODEL",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
        )
        self.client = boto3.client("bedrock-runtime", region_name=region)

    def run(self, task: str, on_tool_call) -> list[str]:
        """Drive the converse loop; on_tool_call(tool, params, confidence)
        returns the tool-result text (which may be a governance-pending note)."""
        messages = [{"role": "user", "content": [{"text": task}]}]
        transcript: list[str] = []
        for _ in range(8):  # hard iteration cap: governance principle, not a nicety
            resp = self.client.converse(
                modelId=self.model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=messages,
                toolConfig=_bedrock_tool_config(),
                inferenceConfig={"maxTokens": 1024, "temperature": 0},
            )
            msg = resp["output"]["message"]
            messages.append(msg)
            tool_uses = [c["toolUse"] for c in msg["content"] if "toolUse" in c]
            for c in msg["content"]:
                if "text" in c and c["text"].strip():
                    transcript.append(c["text"].strip())
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                params = dict(tu["input"])
                confidence = float(params.pop("confidence", 0.7))
                outcome_text = on_tool_call(tu["name"], params, confidence)
                results.append({"toolResult": {"toolUseId": tu["toolUseId"],
                                               "content": [{"text": outcome_text}]}})
            messages.append({"role": "user", "content": results})
        return transcript


# ---------------------------------------------------------------- harness

def run_task(policy: Policy, task: str, session_id: str | None = None) -> dict:
    """Run one task through the governed agent. Returns a step-by-step report."""
    session_id = session_id or f"sess-{uuid.uuid4().hex[:8]}"
    mode = os.environ.get("AUTONOMYGATE_AGENT", "scripted").lower()
    steps: list[dict] = []

    def governed_call(tool: str, params: dict, confidence: float) -> str:
        affected = T.estimate_affected_count(tool, params)
        res = run_evaluation(policy, EvaluateRequest(
            agent_id=AGENT_ID, session_id=session_id, tool=tool, params=params,
            affected_count=affected, model_confidence=confidence,
            preview=f"{tool} affecting {affected} record(s): {params}",
        ))
        step = {"tool": tool, "params": params, "affected_count": affected,
                "route": res.route.value, "risk": res.risk,
                "action_id": res.action_id, "ticket_id": res.ticket_id,
                "matched_override": res.matched_override}
        if res.route is Route.AUTONOMOUS:
            result, _ = T.execute_tool(tool, params)
            from ..storage.repo import get_repo
            get_repo().update_audit_outcome(res.action_id, "executed", "system")
            step["result"] = result
            outcome_text = json.dumps(result, default=str)[:2000]
        else:
            step["result"] = None
            outcome_text = (f"GOVERNANCE HOLD: this action was routed to "
                            f"{res.route.value} (ticket {res.ticket_id}). It will "
                            f"only execute if a human approves it. Do not retry.")
        steps.append(step)
        return outcome_text

    if mode == "bedrock":
        transcript = BedrockPlanner().run(task, governed_call)
    else:
        transcript = []
        for proposal in _scripted_plan(task):
            note = governed_call(proposal["tool"], proposal["params"],
                                 proposal["confidence"])
            transcript.append(note if steps[-1]["route"] != "AUTONOMOUS"
                              else f"Executed {proposal['tool']}.")

    return {"session_id": session_id, "agent_mode": mode, "task": task,
            "steps": steps, "transcript": transcript}

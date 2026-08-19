"""Drop-in governance for ANY agent framework (LangChain, LangGraph, CrewAI,
raw tool loops...) in ~30 lines: wrap each tool with @governed and every call
routes through AutonomyGate before executing.

    GATE = "https://qg37onlhnh.execute-api.us-east-1.amazonaws.com"

    @governed(tool="send_slack_message", affected=lambda p: 1)
    def send_slack_message(channel: str, text: str): ...

Behavior:
  AUTONOMOUS -> your function runs immediately.
  CONFIRM/REVIEW -> GovernanceHold is raised carrying the ticket id; your
  harness surfaces it, a human decides in the AutonomyGate dashboard, and on
  approval your harness re-calls execute_if_approved(). Denials stay denied.
"""
from __future__ import annotations

import functools
import os

import httpx

GATE = os.environ.get("AUTONOMYGATE_URL",
                      "https://qg37onlhnh.execute-api.us-east-1.amazonaws.com")
AGENT_ID = os.environ.get("AGENT_ID", "external-agent")


class GovernanceHold(Exception):
    def __init__(self, route: str, ticket_id: str, reason: str):
        super().__init__(f"{route}: {reason} (ticket {ticket_id})")
        self.route, self.ticket_id, self.reason = route, ticket_id, reason


def governed(tool: str, affected=lambda params: 1, confidence=lambda params: 0.8):
    """Wrap any function so AutonomyGate evaluates it before it runs."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(session_id: str = "default", **params):
            verdict = httpx.post(f"{GATE}/evaluate", json={
                "agent_id": AGENT_ID, "session_id": session_id, "tool": tool,
                "params": params, "affected_count": affected(params),
                "model_confidence": confidence(params),
            }, timeout=30).raise_for_status().json()
            if verdict["route"] != "AUTONOMOUS":
                raise GovernanceHold(verdict["route"], verdict["ticket_id"],
                                     verdict["reason"])
            result = fn(**params)
            httpx.post(f"{GATE}/audit/{verdict['action_id']}/outcome",
                       json={"outcome": "executed"}, timeout=30)
            return result
        wrapper.execute_if_approved = _make_resume(fn)
        return wrapper
    return decorator


def _make_resume(fn):
    def execute_if_approved(ticket_id: str, **params):
        ticket = httpx.get(f"{GATE}/tickets/{ticket_id}", timeout=30)\
            .raise_for_status().json()
        if ticket["status"] != "approved":
            raise GovernanceHold("DENIED_OR_PENDING", ticket_id,
                                 f"status={ticket['status']}")
        result = fn(**params)
        httpx.post(f"{GATE}/audit/{ticket['action_id']}/outcome",
                   json={"outcome": "executed"}, timeout=30)
        return result
    return execute_if_approved


if __name__ == "__main__":
    # Demo: a tool AutonomyGate has never seen, from a stack it doesn't know.
    @governed(tool="send_slack_message")
    def send_slack_message(channel: str, text: str):
        print(f"[slack] {channel}: {text}")
        return {"ok": True}

    try:
        send_slack_message(session_id="ext-demo", channel="#ops",
                           text="deploy finished")
    except GovernanceHold as hold:
        print(f"held by governance -> {hold}")
        print("a human decides in the dashboard; then call "
              f"send_slack_message.execute_if_approved('{hold.ticket_id}', ...)")

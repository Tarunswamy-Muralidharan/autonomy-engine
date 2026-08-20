"""Regression tests for the ticket/audit disclosure chain (external review).

The proven walk: GET /audit (open) leaked ticket_ids -> GET /tickets/{id}
(open) returned RAW params -> unauthenticated PII disclosure, bypassing the
/queue lock. These tests pin the fix: with a reviewer token configured, the
audit log requires auth and an unauthenticated ticket read is status-only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-reviewer-token-for-disclosure-suite"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    os.environ["AUTONOMYGATE_STORAGE"] = "sqlite"
    os.environ["AUTONOMYGATE_DB"] = str(tmp_path / "disc.db")
    os.environ["AUTONOMYGATE_AGENT"] = "scripted"
    from app.storage.repo import reset_repo
    from app.agent.tools import reset_crm
    reset_repo()
    reset_crm()
    import app.main as main
    # Simulate production: a reviewer token IS configured.
    monkeypatch.setattr(main, "REVIEWER_TOKEN", TOKEN)
    return TestClient(main.app)


def _make_held_ticket(client) -> str:
    """Propose an action carrying PII that lands in CONFIRM/REVIEW."""
    resp = client.post("/evaluate", json={
        "agent_id": "disclosure-probe", "session_id": "disc-1",
        "tool": "send_email",
        "params": {"to": "priya.raman@gmail.com",
                   "body": "card 4111 1111 1111 1111"},
        "affected_count": 1, "model_confidence": 0.9,
    })
    assert resp.status_code == 200
    ticket_id = resp.json()["ticket_id"]
    assert ticket_id, "expected the action to be held"
    return ticket_id


def test_unauthenticated_ticket_read_is_status_only(client):
    ticket_id = _make_held_ticket(client)
    resp = client.get(f"/tickets/{ticket_id}")
    assert resp.status_code == 200
    body = resp.json()
    # The integration contract survives: status is pollable.
    assert body["status"] == "pending"
    assert body["ticket_id"] == ticket_id
    # The sensitive payload does not leak.
    assert "params" not in body
    assert "preview" not in body
    assert "4111" not in resp.text
    assert "priya" not in resp.text.lower()


def test_reviewer_ticket_read_is_full(client):
    ticket_id = _make_held_ticket(client)
    resp = client.get(f"/tickets/{ticket_id}",
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    # Approval must execute exactly what was scored: the reviewer sees raw params.
    assert resp.json()["params"]["to"] == "priya.raman@gmail.com"


def test_audit_requires_reviewer_when_token_set(client):
    _make_held_ticket(client)
    assert client.get("/audit").status_code == 401
    resp = client.get("/audit", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_wrong_token_gets_neither(client):
    ticket_id = _make_held_ticket(client)
    bad = {"Authorization": "Bearer wrong-token"}
    assert client.get("/audit", headers=bad).status_code == 401
    body = client.get(f"/tickets/{ticket_id}", headers=bad).json()
    assert "params" not in body  # wrong token degrades to the public view


def test_disclosure_chain_is_dead(client):
    """The original end-to-end walk must fail at its first step."""
    _make_held_ticket(client)
    assert client.get("/audit").status_code == 401

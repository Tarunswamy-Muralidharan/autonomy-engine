"""End-to-end governed agent flow (scripted planner; no cloud dependency).

Proves the full loop the demo shows: task in -> tool proposals -> governance
routing -> held actions execute ONLY after human approval.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["AUTONOMYGATE_STORAGE"] = "sqlite"
    os.environ["AUTONOMYGATE_DB"] = str(tmp_path / "test.db")
    os.environ["AUTONOMYGATE_AGENT"] = "scripted"
    from app.storage.repo import reset_repo
    from app.agent.tools import reset_crm
    reset_repo()
    reset_crm()
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


def test_read_task_executes_autonomously(client):
    r = client.post("/agent/task", json={"task": "What plan is customer 1005 on?"})
    assert r.status_code == 200
    run = r.json()
    assert len(run["steps"]) == 1
    step = run["steps"][0]
    assert step["tool"] == "crm_read"
    assert step["route"] == "AUTONOMOUS"
    assert step["result"]["customer_id"] == "1005"


def test_update_task_waits_for_confirmation_then_executes(client):
    r = client.post("/agent/task",
                    json={"task": "Update customer 1003 plan to enterprise"})
    step = r.json()["steps"][0]
    assert step["tool"] == "crm_update"
    assert step["route"] == "CONFIRM"
    assert step["result"] is None                      # nothing executed yet

    # the record is untouched while the ticket is pending
    from app.agent.tools import get_crm
    assert get_crm().customers["1003"]["plan"] != "enterprise"

    # human approves -> the update actually happens
    d = client.post(f"/tickets/{step['ticket_id']}/decision",
                    json={"decision": "approve", "decided_by": "demo-user"})
    assert d.status_code == 200
    assert d.json()["execution_result"]["new_value"] == "enterprise"
    assert get_crm().customers["1003"]["plan"] == "enterprise"


def test_bulk_cleanup_routes_to_review_and_denial_blocks(client):
    r = client.post("/agent/task", json={"task": "Clean up all inactive customers"})
    step = r.json()["steps"][0]
    assert step["tool"] == "db_delete"
    assert step["route"] == "REVIEW"
    assert step["affected_count"] > 5

    from app.agent.tools import get_crm
    before = len(get_crm().customers)

    d = client.post(f"/tickets/{step['ticket_id']}/decision",
                    json={"decision": "reject", "decided_by": "reviewer-1",
                          "note": "No bulk deletion during audit season."})
    assert d.status_code == 200
    assert len(get_crm().customers) == before          # nothing was deleted

    audit = client.get("/audit", params={"session_id": r.json()["session_id"]}).json()
    assert audit[0]["final_outcome"] == "denied"
    assert audit[0]["matched_override"] == "bulk_delete_always_review"


def test_export_all_hits_hard_override(client):
    r = client.post("/agent/task", json={"task": "Export all customer data"})
    step = r.json()["steps"][0]
    assert step["tool"] == "export_report"
    assert step["route"] == "REVIEW"
    assert step["matched_override"] == "full_export_review"

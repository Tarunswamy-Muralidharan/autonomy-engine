"""Tests mapping 1:1 to Aivar PS-9.1 success criteria, plus robustness checks.

PS-9.1 success criteria:
  1. A bulk database delete (high risk) routes to full review.
  2. A single record update (medium risk) routes to user confirmation.
  3. A read-only query (low risk) executes autonomously.
  4. Risk score breakdown is accurate and human-readable in the audit log.
"""
import concurrent.futures
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["AUTONOMYGATE_STORAGE"] = "sqlite"
    os.environ["AUTONOMYGATE_DB"] = str(tmp_path / "test.db")
    from app.storage.repo import reset_repo
    reset_repo()
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


def _evaluate(client, **kwargs):
    body = {
        "agent_id": "agent-test",
        "session_id": "sess-1",
        "tool": "crm_read",
        "params": {},
        "affected_count": 1,
        "model_confidence": 0.8,
    }
    body.update(kwargs)
    r = client.post("/evaluate", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- Success criterion 1: bulk delete -> REVIEW ---

def test_bulk_delete_routes_to_review(client):
    res = _evaluate(client, tool="db_delete",
                    params={"record_ids": list(range(500))}, affected_count=500)
    assert res["route"] == "REVIEW"
    assert res["matched_override"] == "bulk_delete_always_review"
    assert res["ticket_id"] is not None


def test_small_delete_is_not_hard_blocked(client):
    res = _evaluate(client, tool="db_delete",
                    params={"record_ids": [1, 2, 3, 4, 5]}, affected_count=5)
    assert res["matched_override"] is None       # override must NOT fire for 5 records
    assert res["route"] in ("CONFIRM", "REVIEW")  # still risky, but score-driven


# --- Success criterion 2: single update -> CONFIRM ---

def test_single_update_routes_to_confirm(client):
    res = _evaluate(client, tool="crm_update",
                    params={"customer_id": "1042", "field": "email",
                            "value": "new@example.com"})
    assert res["route"] == "CONFIRM"
    assert res["ticket_id"] is not None


# --- Success criterion 3: read-only -> AUTONOMOUS ---

def test_read_routes_autonomous(client):
    res = _evaluate(client, tool="crm_read", params={"customer_id": "1042"})
    assert res["route"] == "AUTONOMOUS"
    assert res["ticket_id"] is None


# --- Success criterion 4: audit breakdown accurate + human-readable ---

def test_audit_breakdown_readable(client):
    _evaluate(client, tool="crm_update", session_id="sess-audit",
              params={"customer_id": "7", "field": "plan", "value": "gold"})
    records = client.get("/audit", params={"session_id": "sess-audit"}).json()
    assert len(records) == 1
    rec = records[0]
    rb = rec["risk_breakdown"]
    for dim in ("reversibility", "data_scope", "regulatory", "confidence", "total"):
        assert dim in rb and isinstance(rb[dim], (int, float))
    assert rec["route"] == "CONFIRM"
    assert isinstance(rec["reason"], str) and len(rec["reason"]) > 10
    # weighted sum check: total consistent with dimensions and policy weights
    from app.main import policy
    w = policy.weights
    expected = (rb["reversibility"] * w["reversibility"] + rb["data_scope"] * w["data_scope"]
                + rb["regulatory"] * w["regulatory"] + rb["confidence"] * w["confidence"])
    assert abs(expected - rb["total"]) < 1.0


# --- Hard overrides beyond the mandated three ---

def test_external_email_review_internal_email_not(client):
    ext = _evaluate(client, tool="send_email",
                    params={"to": "victim@gmail.com", "body": "hi"})
    assert ext["route"] == "REVIEW"
    assert ext["matched_override"] == "external_email_review"

    internal = _evaluate(client, tool="send_email",
                         params={"to": "teammate@ourcorp.com", "body": "hi"})
    assert internal["matched_override"] is None


# --- Ticket lifecycle: rejection blocks, double-decide conflicts ---

def test_confirm_reject_flow(client):
    res = _evaluate(client, tool="crm_update",
                    params={"customer_id": "9", "field": "plan", "value": "x"})
    tid = res["ticket_id"]
    r = client.post(f"/tickets/{tid}/decision",
                    json={"decision": "reject", "decided_by": "tester", "note": "no"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    # audit outcome reflects the denial
    audit = client.get("/audit", params={"session_id": "sess-1"}).json()
    rec = next(a for a in audit if a["action_id"] == res["action_id"])
    assert rec["final_outcome"] == "denied"

    # double-decide is a conflict, not a silent overwrite
    r2 = client.post(f"/tickets/{tid}/decision", json={"decision": "approve"})
    assert r2.status_code == 409


# --- PII redaction in the audit log ---

def test_pii_redacted_in_audit(client):
    _evaluate(client, tool="crm_update", session_id="sess-pii",
              params={"customer_id": "1", "field": "email",
                      "value": "secret.person@gmail.com"})
    rec = client.get("/audit", params={"session_id": "sess-pii"}).json()[0]
    flat = str(rec["params_redacted"])
    assert "secret.person@gmail.com" not in flat
    assert "[REDACTED:EMAIL]" in flat


# --- Calibration (bonus): repeated approvals lower risk toward autonomous ---

def test_calibration_drifts_down_after_consistent_approvals(client):
    for _ in range(10):
        res = _evaluate(client, tool="crm_update",
                        params={"customer_id": "2", "field": "plan", "value": "y"})
        assert res["route"] == "CONFIRM"
        client.post(f"/tickets/{res['ticket_id']}/decision", json={"decision": "approve"})
    stats = client.get("/calibration/crm_update").json()
    assert stats["confirms"] >= 10
    assert stats["current_adjustment"] == -10.0
    # next evaluation carries the negative adjustment
    res = _evaluate(client, tool="crm_update",
                    params={"customer_id": "2", "field": "plan", "value": "z"})
    assert res["route"] in ("AUTONOMOUS", "CONFIRM")  # 32.5 - 10 = 22.5 -> AUTONOMOUS
    assert res["route"] == "AUTONOMOUS"


# --- Bonus: reviewer MODIFY action — edited params execute, agent's don't ---

def test_modify_decision_executes_edited_params(client):
    res = _evaluate(client, tool="crm_update", session_id="sess-mod",
                    params={"customer_id": "1001", "field": "plan", "value": "enterprise"})
    assert res["route"] == "CONFIRM"
    r = client.post(f"/tickets/{res['ticket_id']}/decision",
                    json={"decision": "modify", "decided_by": "reviewer",
                          "note": "downgrade to silver instead",
                          "edited_params": {"customer_id": "1001", "field": "plan",
                                            "value": "silver"}})
    assert r.status_code == 200
    assert r.json()["execution_result"]["new_value"] == "silver"   # human's edit won
    rec = client.get("/audit", params={"session_id": "sess-mod"}).json()[0]
    assert rec["final_outcome"] == "executed_modified"

    # modify without edited_params is rejected
    res2 = _evaluate(client, tool="crm_update", session_id="sess-mod",
                     params={"customer_id": "1002", "field": "plan", "value": "gold"})
    r2 = client.post(f"/tickets/{res2['ticket_id']}/decision",
                     json={"decision": "modify"})
    assert r2.status_code == 422


# --- Bonus: consistent modifications RAISE risk (adjustment +15) ---

def test_calibration_drifts_up_after_consistent_modifications(client):
    for i in range(10):
        res = _evaluate(client, tool="crm_update",
                        params={"customer_id": str(1001 + i), "field": "plan",
                                "value": "enterprise"})
        client.post(f"/tickets/{res['ticket_id']}/decision",
                    json={"decision": "modify",
                          "edited_params": {"customer_id": str(1001 + i),
                                            "field": "plan", "value": "silver"}})
    stats = client.get("/calibration/crm_update").json()
    assert stats["modifications"] >= 10
    assert stats["current_adjustment"] == 15.0


# --- Concurrency: 20 parallel evaluations all succeed and all audited ---

def test_concurrent_evaluations(client):
    def call(i):
        return _evaluate(client, session_id="sess-conc", tool="crm_read",
                         params={"customer_id": str(i)})
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(call, range(20)))
    assert all(r["route"] == "AUTONOMOUS" for r in results)
    audit = client.get("/audit", params={"session_id": "sess-conc", "limit": 200}).json()
    assert len(audit) == 20


# --- External integration: engine governs tools it does not host ---

def test_external_tool_governed_never_executed_locally(client):
    res = _evaluate(client, tool="send_slack_message", session_id="ext",
                    params={"channel": "#ops", "text": "hello"})
    assert res["route"] in ("CONFIRM", "REVIEW")   # unknown tools score conservatively

    d = client.post(f"/tickets/{res['ticket_id']}/decision",
                    json={"decision": "approve", "decided_by": "reviewer"})
    assert d.status_code == 200                     # was a 500 before the fix
    assert d.json()["execution_result"] is None     # engine did NOT try to execute
    rec = client.get("/audit", params={"session_id": "ext"}).json()[0]
    assert rec["final_outcome"] == "approved_pending_external_execution"

    # the external stack executes on its side, then reports back
    r = client.post(f"/audit/{res['action_id']}/outcome",
                    json={"outcome": "executed", "decided_by": "external-stack"})
    assert r.status_code == 200


# --- Robustness: oversized payloads and redaction performance ---

def test_giant_payload_rejected_fast(client):
    import time
    start = time.time()
    r = client.post("/evaluate", json={
        "agent_id": "x", "session_id": "big", "tool": "crm_read",
        "params": {"blob": "A" * 600_000}, "affected_count": 1,
        "model_confidence": 0.5})
    assert r.status_code == 413
    assert time.time() - start < 5                  # hung for minutes before the fix

def test_redaction_linear_on_large_digitless_text():
    import time
    from app.audit.redact import redact_text
    start = time.time()
    out = redact_text("A" * 150_000)
    assert time.time() - start < 2
    assert "TRUNCATED" in out

def test_audit_limit_clamped(client):
    assert client.get("/audit", params={"limit": -1}).status_code == 200
    assert client.get("/audit", params={"limit": 999999}).status_code == 200


# --- Health ---

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"

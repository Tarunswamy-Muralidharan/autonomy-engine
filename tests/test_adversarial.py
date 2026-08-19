"""Regression tests for defects found by adversarial testing.

Every test here corresponds to a bypass, crash, or fail-open path that was
PROVEN against the running system by black-box red-teaming and white-box
audit. They exist so those defects can never silently return.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["AUTONOMYGATE_STORAGE"] = "sqlite"
    os.environ["AUTONOMYGATE_DB"] = str(tmp_path / "adv.db")
    os.environ["AUTONOMYGATE_AGENT"] = "scripted"
    from app.storage.repo import reset_repo
    from app.agent.tools import reset_crm
    reset_repo()
    reset_crm()
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _ev(client, **kw):
    body = {"agent_id": "adv", "session_id": "adv-1", "tool": "crm_read",
            "params": {}, "affected_count": 1, "model_confidence": 0.8}
    body.update(kw)
    return client.post("/evaluate", json=body)


# === GOVERNANCE BYPASSES (each was proven exploitable) ===

def test_multi_recipient_email_cannot_mask_an_external_address(client):
    """A single internal recipient used to satisfy the 'internal' test and
    let an external address through with no human review."""
    for to in ("attacker@evil.com, victim@ourcorp.com",
               "attacker@evil.com;victim@ourcorp.com",
               '"attacker@evil.com"@ourcorp.com',
               "attacker@evil.com"):
        r = _ev(client, tool="send_email", params={"to": to, "body": "x"}).json()
        assert r["route"] == "REVIEW", f"external recipient slipped through: {to}"
        assert r["matched_override"] == "external_email_review"

    internal = _ev(client, tool="send_email",
                   params={"to": "a@ourcorp.com, b@ourcorp.com", "body": "x"}).json()
    assert internal["matched_override"] is None   # all-internal still flows normally


def test_understated_affected_count_cannot_dodge_the_bulk_override(client):
    """An agent declared affected_count=1 while passing 5000 record ids,
    turning a mandatory REVIEW into a one-click CONFIRM."""
    r = _ev(client, tool="db_delete",
            params={"record_ids": [str(i) for i in range(5000)]},
            affected_count=1).json()
    assert r["route"] == "REVIEW"
    assert r["matched_override"] == "bulk_delete_always_review"
    assert r["risk"]["data_scope"] > 50      # scored on observed, not declared


def test_tool_name_variants_cannot_evade_overrides(client):
    """Overrides matched exact lowercase names only."""
    for variant in ("DB_DELETE", "Db_Delete", " db_delete", "db_delete\n",
                    "db_delete "):
        r = _ev(client, tool=variant,
                params={"record_ids": [str(i) for i in range(500)]},
                affected_count=500).json()
        assert r["route"] == "REVIEW", f"evaded via {variant!r}"
        assert r["matched_override"] == "bulk_delete_always_review"


def test_export_scope_variants_and_missing_scope_fail_closed(client):
    for scope in ("all", "ALL", "All", "all ", " * "):
        r = _ev(client, tool="export_report", params={"scope": scope}).json()
        assert r["route"] == "REVIEW", f"evaded via scope={scope!r}"
    missing = _ev(client, tool="export_report", params={}).json()
    assert missing["route"] == "REVIEW"      # tool would have exported everything


def test_self_reported_confidence_cannot_reach_autonomous_alone(client):
    """confidence=1.0 used to flip a CONFIRM-tier update to AUTONOMOUS."""
    r = _ev(client, tool="crm_update",
            params={"customer_id": "1001", "field": "plan", "value": "gold"},
            model_confidence=1.0).json()
    assert r["route"] == "CONFIRM"
    assert r["risk"]["confidence"] >= 10     # dimension is floored


def test_modify_cannot_escalate_beyond_the_ticket_tier(client):
    """A 3-record delete was 'modified' into a 5000-record delete at approval
    time, skipping the bulk-delete override entirely."""
    res = _ev(client, tool="db_delete", params={"record_ids": ["1", "2", "3"]},
              affected_count=3).json()
    assert res["route"] == "CONFIRM"
    r = client.post(f"/tickets/{res['ticket_id']}/decision", json={
        "decision": "modify", "decided_by": "reviewer",
        "edited_params": {"record_ids": [str(i) for i in range(5000)]}})
    assert r.status_code == 409
    assert r.json()["detail"]["matched_override"] == "bulk_delete_always_review"


def test_proposing_agent_cannot_approve_its_own_action(client):
    res = _ev(client, agent_id="malicious-agent", tool="send_email",
              params={"to": "exfil@evil.com", "body": "data"}).json()
    assert res["route"] == "REVIEW"
    r = client.post(f"/tickets/{res['ticket_id']}/decision",
                    json={"decision": "approve", "decided_by": "malicious-agent"})
    assert r.status_code == 403                     # separation of duties
    other = client.post(f"/tickets/{res['ticket_id']}/decision",
                        json={"decision": "approve", "decided_by": "human-reviewer"})
    assert other.status_code == 200


def test_calibration_cannot_rescue_a_review_tier_action(client):
    """Earned trust used to demote 100-record deletes from REVIEW to CONFIRM."""
    from app.engine.router import evaluate_action
    from app.main import policy
    v = evaluate_action(policy, tool="db_delete", params={}, affected_count=100,
                        model_confidence=0.5, calibration_adjustment=-20)
    assert v.route.value == "REVIEW"


def test_calibration_adjustment_is_clamped_at_the_boundary(client):
    from app.engine.router import evaluate_action
    from app.main import policy
    v = evaluate_action(policy, tool="db_delete", params={}, affected_count=100,
                        model_confidence=0.1, calibration_adjustment=-1000)
    assert v.route.value == "REVIEW"          # absurd adjustment cannot unlock


def test_audit_outcomes_are_write_once(client):
    res = _ev(client, tool="crm_read", params={"customer_id": "1001"}).json()
    first = client.post(f"/audit/{res['action_id']}/outcome",
                        json={"outcome": "executed"})
    assert first.status_code in (200, 409)    # autonomous path may self-record
    contradict = client.post(f"/audit/{res['action_id']}/outcome",
                             json={"outcome": "failed"})
    assert contradict.status_code == 409      # history cannot be rewritten


# === FAIL-OPEN CRASHES (a 500 meant unscored, unrouted, unaudited) ===

@pytest.mark.parametrize("payload", [
    {"session_id": ""},                                   # empty key attribute
    {"tool": ""},
    {"agent_id": ""},
    {"tool": "T" * 3000},                                 # oversized key
    {"session_id": "S" * 3000},
    {"affected_count": -5},                               # negative blast radius
    {"affected_count": 10 ** 40},                         # beyond DynamoDB numbers
    {"params": {"deep": None}},
])
def test_hostile_inputs_are_rejected_not_crashed(client, payload):
    r = _ev(client, **payload)
    assert r.status_code != 500, f"fail-open crash on {payload}"


def test_deeply_nested_params_rejected(client):
    nested = current = {}
    for _ in range(60):
        current["a"] = {}
        current = current["a"]
    r = _ev(client, params=nested)
    assert r.status_code == 422


def test_non_finite_numbers_rejected(client):
    import json as _json
    body = {"agent_id": "adv", "session_id": "adv-nan", "tool": "crm_read",
            "params": {}, "affected_count": 1, "model_confidence": float("nan")}
    r = client.post("/evaluate", content=_json.dumps(body),
                    headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_unknown_tool_never_invokes_arbitrary_attributes(client):
    """A proposed tool named __init__ was routed AUTONOMOUS and, on execute,
    silently reset the CRM."""
    from app.agent.tools import execute_tool, get_crm
    before = len(get_crm().customers)
    for name in ("__init__", "__class__", "not_a_tool"):
        with pytest.raises(ValueError):
            execute_tool(name, {})
    assert len(get_crm().customers) == before


def test_policy_file_is_validated_at_load(client):
    """A malformed override used to fail OPEN (silently never firing)."""
    from app.engine.scorer import Policy
    import tempfile, pathlib, yaml
    good = yaml.safe_load(open(Policy().__class__ and
                               pathlib.Path("app/engine/policy.yaml"),
                               encoding="utf-8"))
    good["hard_overrides"].append({"name": "broken", "tool": "db_delete",
                                   "route": "NOT_A_ROUTE"})
    tmp = pathlib.Path(tempfile.mkdtemp()) / "bad.yaml"
    tmp.write_text(yaml.safe_dump(good), encoding="utf-8")
    with pytest.raises(ValueError):
        Policy(tmp)

"""End-to-end proof that AutonomyGate closes salami-slicing.

A single 101-record delete is caught by the hard override. 100 single-record
deletes in one session must not be, and the 101st must trip the same wire.
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
    from app.storage.repo import reset_repo
    reset_repo()
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


def test_101st_single_record_delete_in_one_session_routes_to_review(client):
    session_id = "salami-session"
    body = {
        "agent_id": "agent-test",
        "session_id": session_id,
        "tool": "db_delete",
        "params": {"record_ids": ["x"]},
        "affected_count": 1,
        "model_confidence": 0.8,
    }
    for i in range(100):
        body["params"] = {"record_ids": [str(i)]}
        r = client.post("/evaluate", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["matched_override"] != "bulk_delete_per_session"

    body["params"] = {"record_ids": ["101"]}
    last = client.post("/evaluate", json=body)
    assert last.status_code == 200, last.text
    assert last.json()["route"] == "REVIEW"
    assert last.json()["matched_override"] == "bulk_delete_per_session"

    fresh = client.post("/evaluate", json={
        "agent_id": "agent-test",
        "session_id": "fresh-session",
        "tool": "db_delete",
        "params": {"record_ids": ["0"]},
        "affected_count": 1,
        "model_confidence": 0.8,
    })
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["matched_override"] != "bulk_delete_per_session"

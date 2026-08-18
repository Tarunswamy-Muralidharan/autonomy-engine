"""DynamoDB implementation of the Repo interface (production backend).

Tables (create with scripts/create_tables.py):
  autonomygate-audit    PK action_id (S); GSI session-index on session_id
  autonomygate-tickets  PK ticket_id (S); GSI status-index on status
  autonomygate-calib    PK action_type (S)

Numbers are stored as Decimal by DynamoDB; we normalize to float on read.
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

from .repo import Repo

PREFIX = os.environ.get("AUTONOMYGATE_TABLE_PREFIX", "autonomygate")


def _clean(obj):
    """Decimal -> float for JSON friendliness."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _ddb_safe(obj):
    """float -> Decimal (DynamoDB rejects float)."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


class DynamoRepo(Repo):
    def __init__(self):
        region = os.environ.get("AWS_REGION", "ap-south-1")
        ddb = boto3.resource("dynamodb", region_name=region)
        self.audit = ddb.Table(f"{PREFIX}-audit")
        self.tickets = ddb.Table(f"{PREFIX}-tickets")
        self.calib = ddb.Table(f"{PREFIX}-calib")

    # --- audit ---
    def put_audit(self, record: dict) -> None:
        item = _ddb_safe(record)
        item.setdefault("outcome", "pending")
        item.setdefault("decided_by", "system")
        self.audit.put_item(Item=item)

    def update_audit_outcome(self, action_id: str, outcome: str, decided_by: str) -> bool:
        try:
            self.audit.update_item(
                Key={"action_id": action_id},
                UpdateExpression="SET outcome = :o, decided_by = :d",
                ConditionExpression="attribute_exists(action_id)",
                ExpressionAttributeValues={":o": outcome, ":d": decided_by},
            )
            return True
        except self.audit.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def query_audit(self, session_id=None, agent_id=None, limit=100):
        if session_id:
            resp = self.audit.query(
                IndexName="session-index",
                KeyConditionExpression=Key("session_id").eq(session_id),
                Limit=limit, ScanIndexForward=False,
            )
        else:
            kwargs = {"Limit": limit}
            if agent_id:
                kwargs["FilterExpression"] = Attr("agent_id").eq(agent_id)
            resp = self.audit.scan(**kwargs)
        items = [_clean(i) for i in resp.get("Items", [])]
        for i in items:
            i["final_outcome"] = i.pop("outcome", "pending")
        return sorted(items, key=lambda r: r.get("ts", 0), reverse=True)[:limit]

    # --- tickets ---
    def put_ticket(self, ticket: dict) -> None:
        item = _ddb_safe(ticket)
        item["status"] = "pending"
        self.tickets.put_item(Item=item)

    def get_ticket(self, ticket_id: str):
        resp = self.tickets.get_item(Key={"ticket_id": ticket_id})
        item = resp.get("Item")
        return _clean(item) if item else None

    def decide_ticket(self, ticket_id: str, decision: str, decided_by: str, note: str = ""):
        try:
            self.tickets.update_item(
                Key={"ticket_id": ticket_id},
                UpdateExpression="SET #s = :s, decided_at = :t, decided_by = :b, note = :n",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": decision, ":t": Decimal(str(time.time())),
                    ":b": decided_by, ":n": note, ":pending": "pending",
                },
            )
        except self.tickets.meta.client.exceptions.ConditionalCheckFailedException:
            return None
        return self.get_ticket(ticket_id)

    def list_tickets(self, status=None, kind=None):
        if status:
            resp = self.tickets.query(
                IndexName="status-index",
                KeyConditionExpression=Key("status").eq(status),
            )
        else:
            resp = self.tickets.scan()
        items = [_clean(i) for i in resp.get("Items", [])]
        if kind:
            items = [i for i in items if i.get("kind") == kind]
        return sorted(items, key=lambda t: t.get("ts", 0), reverse=True)

    # --- calibration ---
    def get_calibration(self, action_type: str) -> dict:
        resp = self.calib.get_item(Key={"action_type": action_type})
        item = resp.get("Item")
        if not item:
            return {"action_type": action_type, "confirms": 0, "approvals": 0,
                    "rejections": 0, "adjustment": 0.0}
        return _clean(item)

    def update_calibration(self, action_type: str, decision: str) -> dict:
        col = "approvals" if decision == "approved" else "rejections"
        self.calib.update_item(
            Key={"action_type": action_type},
            UpdateExpression=f"ADD confirms :one, {col} :one",
            ExpressionAttributeValues={":one": 1},
        )
        return self.get_calibration(action_type)

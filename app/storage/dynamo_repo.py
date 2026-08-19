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
    """Decimal -> int/float for JSON friendliness.

    Whole numbers MUST come back as ints: a ticket stores the caller's raw
    params and hands them back on approval, so turning record id 1000 into
    1000.0 would mean the engine approves one action and returns another.
    """
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
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
        region = os.environ.get("AWS_REGION", "us-east-1")
        ddb = boto3.resource("dynamodb", region_name=region)
        self.audit = ddb.Table(f"{PREFIX}-audit")
        self.tickets = ddb.Table(f"{PREFIX}-tickets")
        self.calib = ddb.Table(f"{PREFIX}-calib")

    # --- audit ---
    def put_audit(self, record: dict) -> None:
        item = _ddb_safe(record)
        item.setdefault("outcome", "pending")
        item.setdefault("decided_by", "system")
        # Append-only: an existing action_id is never overwritten.
        try:
            self.audit.put_item(
                Item=item, ConditionExpression="attribute_not_exists(action_id)")
        except self.audit.meta.client.exceptions.ConditionalCheckFailedException:
            raise ValueError(f"audit record {record.get('action_id')} already exists")

    def update_audit_outcome(self, action_id: str, outcome: str, decided_by: str) -> bool:
        # Conditional write: the outcome may be set once, from `pending`.
        # A check-then-act in the API layer cannot enforce this across
        # concurrent Lambda instances - only the database can.
        try:
            self.audit.update_item(
                Key={"action_id": action_id},
                UpdateExpression="SET outcome = :o, decided_by = :d",
                ConditionExpression="attribute_exists(action_id) AND "
                                    "(attribute_not_exists(outcome) OR outcome = :p)",
                ExpressionAttributeValues={":o": outcome, ":d": decided_by,
                                           ":p": "pending"},
            )
            return True
        except self.audit.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def get_audit(self, action_id: str):
        item = self.audit.get_item(Key={"action_id": action_id},
                                   ConsistentRead=True).get("Item")
        if not item:
            return None
        rec = _clean(item)
        rec["final_outcome"] = rec.pop("outcome", "pending")
        return rec

    def _paginate(self, fn, /, cap: int = 5000, **kwargs) -> list[dict]:
        """Follow LastEvaluatedKey. A single Query/Scan page stops at 1MB and
        truncates SILENTLY - which would drop pending REVIEW tickets out of
        the reviewer's queue with no error at all."""
        items, pages = [], 0
        while pages < 20:
            resp = fn(**kwargs)
            items.extend(resp.get("Items", []))
            key = resp.get("LastEvaluatedKey")
            if not key or len(items) >= cap:
                break
            kwargs["ExclusiveStartKey"] = key
            pages += 1
        return items

    def query_audit(self, session_id=None, agent_id=None, limit=100):
        # Collect the full matching set, THEN sort by time and slice: the
        # session GSI is hash-only, so DynamoDB's own Limit returns an
        # arbitrary page rather than the most recent records.
        if session_id:
            kwargs = {"IndexName": "session-index",
                      "KeyConditionExpression": Key("session_id").eq(session_id)}
            if agent_id:   # was silently ignored on this branch
                kwargs["FilterExpression"] = Attr("agent_id").eq(agent_id)
            raw = self._paginate(self.audit.query, **kwargs)
        else:
            kwargs = {}
            if agent_id:
                kwargs["FilterExpression"] = Attr("agent_id").eq(agent_id)
            raw = self._paginate(self.audit.scan, **kwargs)
        items = [_clean(i) for i in raw]
        for i in items:
            i["final_outcome"] = i.pop("outcome", "pending")
        return sorted(items, key=lambda r: r.get("ts", 0), reverse=True)[:limit]

    # --- tickets ---
    def put_ticket(self, ticket: dict) -> None:
        item = _ddb_safe(ticket)
        item["status"] = "pending"
        self.tickets.put_item(Item=item)

    def get_ticket(self, ticket_id: str):
        # Strongly consistent: a reviewer deciding immediately after the
        # ticket is created must not get a spurious 404, and the
        # separation-of-duties check must not read a stale agent_id.
        resp = self.tickets.get_item(Key={"ticket_id": ticket_id},
                                     ConsistentRead=True)
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
        # Filter server-side and paginate: applying `kind` in Python after a
        # truncated page could return [] while review tickets exist.
        kwargs: dict = {}
        if kind:
            kwargs["FilterExpression"] = Attr("kind").eq(kind)
        if status:
            kwargs.update(IndexName="status-index",
                          KeyConditionExpression=Key("status").eq(status))
            raw = self._paginate(self.tickets.query, **kwargs)
        else:
            raw = self._paginate(self.tickets.scan, **kwargs)
        items = [_clean(i) for i in raw]
        return sorted(items, key=lambda t: t.get("ts", 0), reverse=True)

    # --- calibration ---
    def get_calibration(self, action_type: str) -> dict:
        # Always return the full shape: DynamoDB omits counters that ADD has
        # never touched, so the raw item has fewer keys than the SQLite one.
        stats = {"action_type": action_type, "confirms": 0, "approvals": 0,
                 "rejections": 0, "modifications": 0, "adjustment": 0.0}
        item = self.calib.get_item(Key={"action_type": action_type}).get("Item")
        if item:
            stats.update(_clean(item))
        return stats

    def update_calibration(self, action_type: str, decision: str) -> dict:
        col = {"approved": "approvals", "rejected": "rejections",
               "modified": "modifications"}.get(decision, "rejections")
        self.calib.update_item(
            Key={"action_type": action_type},
            UpdateExpression=f"ADD confirms :one, {col} :one",
            ExpressionAttributeValues={":one": 1},
        )
        return self.get_calibration(action_type)

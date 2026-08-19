"""Mock enterprise toolset governed by AutonomyGate.

A small in-memory CRM with seeded fake customers, plus email/export stubs.
Every tool returns (result, affected_count) so the harness can feed the
risk scorer the true blast radius of the action.
"""
from __future__ import annotations

import random
import time

random.seed(42)

_FIRST = ["Aarav", "Diya", "Rohan", "Priya", "Kabir", "Ananya", "Vikram",
          "Meera", "Arjun", "Sneha"]
_LAST = ["Sharma", "Iyer", "Patel", "Reddy", "Nair", "Das", "Kulkarni",
         "Menon", "Bose", "Rao"]
_PLANS = ["free", "silver", "gold", "enterprise"]


def _seed(n: int = 400) -> dict[str, dict]:
    customers = {}
    for i in range(1000, 1000 + n):
        cid = str(i)
        active = random.random() > 0.3
        customers[cid] = {
            "customer_id": cid,
            "name": f"{random.choice(_FIRST)} {random.choice(_LAST)}",
            "email": f"user{i}@example.com",
            "plan": random.choice(_PLANS),
            "active": active,
            "last_seen_days_ago": random.randint(0, 20) if active else random.randint(90, 400),
        }
    return customers


class MockCRM:
    def __init__(self):
        self.customers = _seed()
        self.sent_emails: list[dict] = []
        self.exports: list[dict] = []
        self.deleted: list[str] = []

    # each tool returns (result, affected_count)

    def crm_read(self, customer_id: str) -> tuple[dict, int]:
        cust = self.customers.get(str(customer_id))
        if not cust:
            return {"error": f"customer {customer_id} not found"}, 0
        return dict(cust), 1

    def crm_search(self, query: str = "", inactive_only: bool = False) -> tuple[list, int]:
        hits = [c for c in self.customers.values()
                if (not inactive_only or not c["active"])
                and (query.lower() in c["name"].lower() or query == "")]
        return hits, len(hits)

    def crm_update(self, customer_id: str, field: str, value: str) -> tuple[dict, int]:
        cust = self.customers.get(str(customer_id))
        if not cust:
            return {"error": f"customer {customer_id} not found"}, 0
        old = cust.get(field)
        cust[field] = value
        return {"customer_id": customer_id, "field": field,
                "old_value": old, "new_value": value}, 1

    def db_delete(self, record_ids: list[str]) -> tuple[dict, int]:
        deleted = [rid for rid in map(str, record_ids) if rid in self.customers]
        for rid in deleted:
            del self.customers[rid]
            self.deleted.append(rid)
        return {"deleted_count": len(deleted)}, len(record_ids)

    def send_email(self, to: str, body: str) -> tuple[dict, int]:
        self.sent_emails.append({"to": to, "body": body, "ts": time.time()})
        return {"sent_to": to}, 1

    def export_report(self, scope: str) -> tuple[dict, int]:   # no implicit "all"
        n = len(self.customers) if scope in ("all", "*") else 1
        self.exports.append({"scope": scope, "rows": n, "ts": time.time()})
        return {"exported_rows": n}, n


# Tool metadata: descriptions double as the LLM's manual (Bedrock toolConfig)
TOOL_SPECS = [
    {"name": "crm_read",
     "description": "Read one customer record by customer_id. Read-only.",
     "schema": {"type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"]}},
    {"name": "crm_search",
     "description": "Search customer records by name substring; set inactive_only "
                    "true to list inactive customers. Read-only.",
     "schema": {"type": "object",
                "properties": {"query": {"type": "string"},
                               "inactive_only": {"type": "boolean"}},
                "required": []}},
    {"name": "crm_update",
     "description": "Update one field of one customer record.",
     "schema": {"type": "object",
                "properties": {"customer_id": {"type": "string"},
                               "field": {"type": "string"},
                               "value": {"type": "string"}},
                "required": ["customer_id", "field", "value"]}},
    {"name": "db_delete",
     "description": "Permanently delete customer records by id list. Irreversible.",
     "schema": {"type": "object",
                "properties": {"record_ids": {"type": "array",
                                              "items": {"type": "string"}}},
                "required": ["record_ids"]}},
    {"name": "send_email",
     "description": "Send an email. 'to' must be a full address.",
     "schema": {"type": "object",
                "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                "required": ["to", "body"]}},
    {"name": "export_report",
     "description": "Export customer data. scope='all' exports every record.",
     "schema": {"type": "object",
                "properties": {"scope": {"type": "string"}},
                "required": ["scope"]}},
]

_crm: MockCRM | None = None


def get_crm() -> MockCRM:
    global _crm
    if _crm is None:
        _crm = MockCRM()
    return _crm


def reset_crm() -> None:
    global _crm
    _crm = None


def is_local_tool(tool: str) -> bool:
    """True if this engine hosts the tool's implementation. External agents
    govern their OWN tools through /evaluate; the engine never executes those."""
    return any(spec["name"] == tool for spec in TOOL_SPECS)


def execute_tool(tool: str, params: dict) -> tuple[object, int]:
    # Allowlist FIRST: `tool` is model-supplied. A bare getattr let a proposed
    # "tool" named __init__ (or any other attribute) be invoked - found in
    # adversarial testing. Only declared tools are ever reachable.
    if not is_local_tool(tool):
        raise ValueError(f"unknown tool: {tool}")
    if not isinstance(params, dict):
        raise ValueError(f"tool params must be an object, got {type(params).__name__}")
    fn = getattr(get_crm(), tool)
    try:
        return fn(**params)
    except TypeError as exc:      # wrong/extra kwargs from the model
        raise ValueError(f"invalid parameters for {tool}: {exc}") from exc


def estimate_affected_count(tool: str, params: dict) -> int:
    """Blast radius BEFORE execution — what the risk scorer needs.

    Fails toward the MAXIMUM radius on anything unexpected: an
    under-estimate would under-govern the action and mislead the human
    reviewer about what they are approving.
    """
    crm = get_crm()
    everything = len(crm.customers)
    if not isinstance(params, dict):
        return everything

    if tool == "db_delete":
        ids = params.get("record_ids")
        if isinstance(ids, (list, tuple, set)):
            return len(ids)
        return everything                       # unknown shape -> worst case

    if tool == "export_report":
        scope = params.get("scope")
        if not isinstance(scope, str):
            return everything                   # MockCRM defaults to exporting all
        return 1 if scope.strip().lower() not in ("all", "*") else everything

    if tool == "crm_search":
        try:
            _, n = crm.crm_search(**{k: v for k, v in params.items()
                                     if k in ("query", "inactive_only")})
            return n
        except Exception:
            return everything
    return 1

# AutonomyGate — Production Roadmap

This document is the honest gap between what AutonomyGate is today — a
production-*shaped* governance prototype with a hardened core — and what an
enterprise could deploy as an **authorization and enforcement layer for
autonomous agents**. It was written in response to an external architecture
review, taken at face value. The phases are ordered by *dependency*, not by
severity: half of the review's findings (multi-tenancy, audit attribution,
per-reviewer auth, contextual calibration) share one missing foundation —
identity — so identity comes first.

The transformation goal, in one sentence from that review:

> Turn "a smart risk-scoring checkpoint" into "an enforcement layer that
> guarantees an agent can execute only the exact operation it was authorized
> to execute."

## Phase 0 — Disclosure fixes (DONE)

- `GET /tickets/{id}` returns a status-only view to unauthenticated callers;
  raw params (the most sensitive store in the system) are reviewer-only.
  Closes the proven chain: open `/audit` → ticket_ids → open ticket reads →
  unredacted PII.
- `GET /audit` is reviewer-only: redacted or not, audit rows expose agent
  identities, tool usage and decision patterns.
- Regression tests in `tests/test_disclosure.py`.

## Phase 1 — Identity & tenancy (module: `app/identity/`)

Today `agent_id` is a self-declared string (impersonation is free) and the
reviewer is one shared token (no attribution). This phase gives agents real
credentials (hashed API keys), reviewers individual tokens so `decided_by`
is a verified identity rather than request-body text, and threads
`tenant_id` through every table, policy lookup and calibration counter.
Endgame: Cognito + API Gateway JWT authorizer; the module is the seam.

## Phase 2 — Policy control plane (module: `app/policystore/`)

Versioned, immutable, validated policy documents in storage; the gate reads
the active version through a short-TTL cache, so a compliance edit is live
in under a minute with no redeploy. Every verdict is stamped with
`policy_version` + `policy_hash`, making historical decisions exactly
reproducible ("which rulebook judged this action?" has one answer).

## Phase 3 — Score the action, not the tool (module: `app/contracts/`)

Tool manifests declare per-field sensitivity and where impact truly comes
from. `crm_update(nickname)` and `crm_update(account_status=terminated)`
stop scoring identically; unknown fields are treated as CRITICAL (fail
closed); filter-shaped operations with no ID list stop being invisible to
blast-radius checks. `observed_blast_radius` remains as the floor — the
manifest refines it, never replaces the defence.

## Phase 4 — Cumulative governance (module: `app/cumulative/`)

Closes the salami-slicing hole: 100 single-record deletes in one session now
trip the same red line as one 100-record delete. Rolling per-session and
per-day ledgers with atomic counters, plus velocity rules (an agent
proposing 100 actions/minute is itself a signal — and a queue-flooding DoS
vector).

## Phase 5 — Signed execution capabilities (module: `app/capability/`)

The centerpiece: on approval, the gate issues a signed, expiring,
single-use capability binding the exact operation — tool, canonical params
hash, tenant, nonce, policy version. The executor SDK runs an operation
only if the capability verifies against the very bytes it is about to
execute. Approval-as-advice becomes approval-as-enforcement. KMS asymmetric
signing in production (executors hold no keys, IAM splits sign from
verify); HMAC fallback for single-trust-domain dev.

## Phase 6 — Human control plane (module: `app/humanplane/`)

Humans don't scale; the queue must respect that. Risk-ranked (not FIFO)
queue, near-duplicate folding with group decisions, SLA escalation, and
expiry-as-denial (an unreviewed ticket fails closed, never sits forever).
Kill switch: pause a tenant or agent and every proposal routes to REVIEW —
observable, auditable, nothing autonomous.

## Phase 7 — Tamper-evident audit (module: `app/auditchain/`)

Today's conditional writes stop *callers* from rewriting history; they do
not stop an attacker with database access. This phase adds per-record
content hashes, periodic Merkle anchors chained to each other, a
verification CLI that pinpoints tampered records, and an export path to a
WORM (Object Lock) S3 bucket — designed for a separate security account.
Tamper-*resistant* becomes tamper-*evident*.

## Phase 8 — Abuse resistance & test maturity

API Gateway usage plans + per-key quotas; an integration suite against
DynamoDB Local so conditional-write and pagination semantics are tested
against the real database, not SQLite's approximation; property-based tests
for the invariant that matters most ("no input combination routes an
override-matching action below its route"); authorization-matrix tests for
every endpoint × principal.

## Deliberately not yet

Multi-region, SOC2 tooling, policy simulation, behavior analytics. Those
are company-building; the phases above are correctness. The order of
operations is the point: identity → versioned policy → action semantics →
cumulative state → enforcement. Built in any other order, the tenancy and
version columns get retrofitted twice.

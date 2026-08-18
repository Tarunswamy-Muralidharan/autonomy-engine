# AutonomyGate — Build Plan for Aivar PS-9.1 (Graduated Autonomy Engine)

Working name: **AutonomyGate** (rename freely). One-liner for the README:

> A production-deployed governance engine that risk-scores every action an AI agent attempts and routes it to the right autonomy level — autonomous execution, user confirmation, or human review — with a full audit trail and adaptive threshold calibration.

---

## 1. What the judges will grade (rubric → our answer)

| Rubric item | Our answer |
|---|---|
| Deployed on AWS, governs AI workloads on AWS | FastAPI engine on **AWS App Runner** (or Lambda), governing a **Bedrock Claude agent** also on AWS |
| Real LLM provider | Amazon Bedrock (Claude) drives the sample agent AND supplies per-action confidence |
| Concurrent requests + persisted state | FastAPI async + **DynamoDB** (audit log, review queue, calibration stats) |
| Usable API | REST endpoints for evaluate / queue / decisions / audit, documented via OpenAPI (`/docs` free with FastAPI) |
| Logging, error handling, health check | CloudWatch structured logs, global exception handlers, `/health` |
| Success criteria | Each one is a pytest test — see section 8 |
| Bonus | Adaptive threshold calibration — see section 9 |

---

## 2. Architecture

```
                        +--------------------------------------+
 User task              |     SAMPLE GOVERNED AGENT            |
 ---------------------> |  Bedrock (Claude) + tool definitions |
                        |  (support-ops agent)                 |
                        +-------------------+------------------+
                                            | every tool call
                                            v
                        +--------------------------------------+
                        |         AUTONOMYGATE ENGINE          |
                        |  1. Risk scorer (4 dimensions)       |
                        |  2. Risk -> autonomy mapping         |
                        |  3. Router:                          |
                        |     LOW    -> execute now            |
                        |     MEDIUM -> preview + confirm      |
                        |     HIGH   -> review queue           |
                        |  4. Audit writer (every decision)    |
                        |  5. Calibration updater (bonus)      |
                        +---+-----------+-----------+----------+
                            |           |           |
                            v           v           v
                     +----------+ +-----------+ +-----------+
                     | EXECUTE  | | CONFIRM   | | REVIEW    |
                     | tool runs| | UI: show  | | QUEUE     |
                     | result   | | preview,  | | approver  |
                     | returned | | approve/  | | decides   |
                     +----------+ | reject    | | later     |
                                  +-----------+ +-----------+
                            all outcomes -> DynamoDB audit log
```

AWS mapping:
- **Engine + agent**: one FastAPI app in one container → AWS **App Runner** (simplest path to a public HTTPS URL with autoscaling + health checks; fallback: Lambda + API Gateway via Mangum if App Runner unavailable).
- **DynamoDB**: 3 tables (audit, queue, calibration). On-demand billing, ~zero cost at demo scale.
- **Bedrock**: Claude (use the cheapest Claude available in your region for the agent; temperature 0).
- **CloudWatch**: logs from App Runner automatically; add structured JSON logging.
- Local dev mirror: `USE_LOCAL=1` swaps DynamoDB for SQLite via a tiny repository interface — never blocked by AWS while building.

## 3. The sample governed workload (needed to prove the engine governs something real)

Support-ops agent (Bedrock Claude, tool-calling) with a mock company database (SQLite seeded with fake customers/orders):

| Tool | What it does | Designed risk profile |
|---|---|---|
| `crm_read(customer_id)` | Read one record | LOW → autonomous |
| `crm_search(query)` | Read many records | LOW/MEDIUM (scope-dependent) |
| `crm_update(customer_id, field, value)` | Update one record | MEDIUM → confirm |
| `db_delete(record_ids[])` | Delete records | MEDIUM if 1, HIGH if bulk |
| `send_email(to, body)` | Send email | MEDIUM internal, HIGH external domain |
| `export_report(scope)` | Export data | HIGH if scope = all customers |

The agent loop intercepts EVERY tool call and sends it to the engine's `/evaluate` before execution. The engine's verdict controls what happens. This makes the demo storyline obvious: same agent, different actions, three different governance outcomes.

## 4. Risk scorer — the heart of the build

Score each action 0–100 as a weighted sum of four dimensions (weights configurable in `policy.yaml`):

**1. Reversibility (weight 35)** — lookup table per tool+operation:
- read = 0 - reversible write (update with old value captured) = 40 - hard delete = 90 - external side effect (email, export) = 80 (cannot be unsent)

**2. Data scope (weight 25)** — how many records/users affected:
- 1 record = 10 - 2–100 = 40 - >100 = 90 - "all" = 100
- Log-scale formula so it is smooth: `min(100, 10 + 25*log10(n))` with n = affected count.

**3. Regulatory category (weight 20)** — per-field/table tags in policy.yaml:
- non-sensitive = 0 - PII fields = 60 - financial/health-tagged tables = 90

**4. Model confidence (weight 20)** — the agent (Claude) must return, with every tool call, a structured self-assessment: `{"confidence": 0.0-1.0, "reasoning": "..."}` via structured output. Risk contribution = `(1 - confidence) * 100`.
- IMPORTANT interview point: self-reported confidence is a WEAK signal (we know this from calibration theory) — that is exactly why it only carries 20% weight and why thresholds are calibrated empirically (bonus).

**Thresholds** (in policy.yaml, not hardcoded — being config is itself a talking point):
- score < 30 → AUTONOMOUS - 30–65 → CONFIRM - > 65 → REVIEW
- Plus **hard overrides** that bypass scoring entirely (deterministic beats probabilistic): bulk delete >100 records → always REVIEW; any action while agent is suspended → always BLOCK. State the principle in the README: "the scorer advises, the policy file rules."

Verify the three mandated scenarios land correctly with these weights:
- Bulk delete 500 records: rev 90*.35 + scope 90*.25 + reg 60*.20 + conf ~20*.20 = ~70 → REVIEW ✓
- Single record update: 40*.35 + 10*.25 + 60*.20 + 20*.20 = ~33 → CONFIRM ✓
- Read-only query: 0 + 10*.25 + 0 + 10*.20 = ~5 → AUTONOMOUS ✓

## 5. Data model (DynamoDB)

**Table `audit`** — every evaluated action, append-only:
`action_id (PK)`, `session_id (GSI)`, `agent_id`, `timestamp`, `tool`, `params_redacted`, `risk_breakdown {rev, scope, reg, conf, total}`, `route (AUTONOMOUS|CONFIRM|REVIEW)`, `matched_override (nullable)`, `final_outcome (executed|confirmed_executed|rejected|approved|denied|pending)`, `decided_by (system|user|reviewer_id)`.

**Table `review_queue`** — `item_id (PK)`, `status (pending|approved|denied) (GSI)`, `action_ref`, `risk_score`, `preview_text`, `submitted_at`, `decided_at`, `reviewer_note`.

**Table `calibration`** — `action_type (PK)`, `confirm_count`, `approve_count`, `reject_count`, `modify_count`, `current_adjustment` (bonus).

## 6. API surface

- `POST /evaluate` — body: agent_id, session_id, tool, params, model_confidence → returns route + risk breakdown + (if CONFIRM/REVIEW) a ticket id. This is the endpoint the agent harness calls.
- `POST /confirm/{ticket_id}` — body: approve|reject (the user-confirmation path).
- `GET /queue?status=pending` / `POST /queue/{id}/decision` — body: approve|deny + note (the reviewer path).
- `GET /audit?session_id=&agent_id=&from=&to=` — queryable audit log.
- `GET /health` — checks DynamoDB + Bedrock reachability, returns build version.
- `POST /agent/task` — give the sample agent a natural-language task (this is what you demo).
- Minimal UI: one static HTML page (vanilla JS polling the API) with three panels — live audit feed, pending confirmations, review queue. Served by FastAPI. No frontend framework: you are a Python candidate; a clean API + simple page beats a broken React app.

## 7. Repo layout

```
autonomy-engine/
  app/
    main.py            # FastAPI app, routers, health
    engine/
      scorer.py        # 4-dimension risk scorer
      router.py        # thresholds + hard overrides
      policy.yaml      # weights, thresholds, overrides, regulatory tags
      calibration.py   # bonus: adaptive thresholds
    agent/
      support_agent.py # Bedrock Claude tool-calling loop
      tools.py         # mock CRM/db/email tools + seed data
    storage/
      repo.py          # interface; dynamo.py + sqlite.py impls
    audit/
      log.py           # structured audit writer + PII redaction of params
  static/index.html    # dashboard
  tests/               # pytest per success criterion (section 8)
  Dockerfile
  deploy/apprunner.md  # exact deploy steps
  README.md            # architecture diagram, tradeoffs, run instructions
  demo/demo_script.md  # the 3-minute recorded demo storyline
```

## 8. Success criteria → tests (write these EARLY, day 2)

1. `test_bulk_delete_routes_to_review` — db_delete with 500 ids → route == REVIEW (both via scorer AND via hard override — assert override recorded).
2. `test_single_update_routes_to_confirm` — crm_update 1 record → CONFIRM.
3. `test_read_routes_autonomous` — crm_read → AUTONOMOUS, executed immediately.
4. `test_audit_breakdown_readable` — audit entry contains all four dimension scores + total + route + human-readable strings.
5. Plus: `test_confirm_reject_blocks_execution`, `test_reviewer_deny_blocks`, `test_expired_pending_actions_never_execute`, `test_concurrent_evaluates` (asyncio gather 20 calls — proves concurrency claim).

## 9. Bonus: adaptive threshold calibration

After every CONFIRM decision, update `calibration` counts per action_type. Rule (simple + explainable — resist ML here):
- If an action type has >= 10 confirmations with >= 90% approval and zero modifications → subtract 10 from its risk score (drifts toward AUTONOMOUS).
- If >= 40% rejections → add 15 (drifts toward REVIEW).
- Cap adjustment at +/-20 so calibration can never overpower hard overrides. Log every adjustment change to the audit table — calibration itself is governed. That sentence goes in the README; it is exactly the kind of line their CTO notices.

## 10. Build schedule (7 days; compress by merging days if deadline is tighter)

- **Day 0 (half day):** AWS account final setup — IAM user, Bedrock model access request (do FIRST, approval can lag), DynamoDB tables, App Runner sanity test with hello-world container. If card/billing blocked: proceed local-first, deploy day 6.
- **Day 1:** Repo skeleton, policy.yaml, scorer + router with hard overrides. Local SQLite storage. Tests 1–3 passing against the scorer directly.
- **Day 2:** FastAPI endpoints + audit writer + PII redaction. All pytest success-criteria tests passing locally.
- **Day 3:** Bedrock agent (support_agent + mock tools + interception wiring). End-to-end local: natural-language task → tool calls → routed outcomes.
- **Day 4:** Confirmation flow + review queue + static dashboard. The three-outcome demo storyline works end to end.
- **Day 5:** DynamoDB implementation behind the repo interface. Dockerfile. Deploy to App Runner. Wire CloudWatch structured logs. `/health` complete.
- **Day 6:** Bonus calibration. Concurrency test. Polish: README with architecture diagram + tradeoffs section, seed data that makes the demo story vivid.
- **Day 7:** Record 3-minute demo video. Dry-run the whole demo twice from a clean browser. Buffer for the inevitable.

**Cut lines if time runs out (in order):** calibration bonus → dashboard polish (CLI confirm is acceptable) → App Runner (fall back to Lambda, or worst case a public EC2/localtunnel with honest README note). NEVER cut: the three-outcome routing, audit log, tests, Bedrock integration, deployment of SOME kind.

## 11. Demo script (3 minutes)

1. Open the deployed URL `/health` — "live on AWS App Runner, backed by DynamoDB and Bedrock." (10s)
2. Task 1: "What is customer 1042's current plan?" → agent reads → dashboard shows AUTONOMOUS, executed, audit entry with score ~5. (30s)
3. Task 2: "Update customer 1042's email to X" → CONFIRM panel lights up with preview → approve → executes. Show audit breakdown. (45s)
4. Task 3: "Clean up all inactive customers" → agent attempts db_delete of 500 → REVIEW queue + hard-override note in audit. Deny it with a note. Agent reports it could not proceed. (45s)
5. Show the audit API query for the whole session; point at the four-dimension breakdown. (20s)
6. One line on calibration: show the calibration table after repeated approvals lowered a score. (20s)
7. Close: "Deterministic overrides beat probabilistic scores; the LLM proposes, policy decides; every decision is auditable." (10s)

## 12. Interview defense cheatsheet

- Why weighted-sum scorer, not an LLM judge? Deterministic, explainable, testable, free, and fast; the LLM contributes exactly one input (confidence) at bounded weight. LLM-as-judge for risk = circular governance.
- Why hard overrides? Regulators and enterprises need guarantees, not probabilities. Score advises; policy rules.
- Confidence is self-reported — trustable? No; that is why it is 20% weight, why thresholds are config, and why calibration adjusts from observed human decisions.
- What breaks at scale? DynamoDB and App Runner autoscale; the bottleneck is Bedrock rate limits and human review throughput — which is why calibration exists: it converts repeatedly-approved action types into autonomous ones, shrinking the human load over time.
- Security of the confirm endpoint? Ticket IDs are unguessable UUIDs; production would add authn (Cognito/OIDC) — noted in README as a known limitation.
- How would this plug into Aivar's stack? It is a policy decision point; Velogent-style pipelines would call /evaluate exactly like the sample agent does — the engine is workload-agnostic by design.

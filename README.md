# AutonomyGate — Graduated Autonomy Engine

**🔴 LIVE DEPLOYMENT (AWS):** https://qg37onlhnh.execute-api.us-east-1.amazonaws.com
(API Gateway → Lambda → DynamoDB, us-east-1 · dashboard at `/` · OpenAPI at `/docs` · health at `/health`)

**Aivar Agentic AI Task — PS-9.1.** A production-deployed governance engine that
risk-scores every action an AI agent attempts and routes it to the right
autonomy level — **autonomous execution**, **user confirmation**, or **human
review** — with a persisted audit trail and adaptive threshold calibration.

```
User task -> Agent (Bedrock Claude) -> proposed tool call
                                            |
                                            v
                                   +------------------+
                                   |   AutonomyGate   |
                                   |  4-dim risk score|
                                   |  + hard overrides|
                                   +--------+---------+
              +-----------------------------+------------------------------+
              v                             v                              v
        AUTONOMOUS                     CONFIRM                         REVIEW
     executes immediately     preview shown; user approves     human review queue;
                              or rejects, then executes        approve / reject
              |                             |                              |
              +-----------------------------+------------------------------+
                                            v
                          append-only, PII-redacted AUDIT LOG (DynamoDB)
```

## How risk is scored

Each proposed action gets a 0–100 score: weighted sum of four dimensions
(weights live in `app/engine/policy.yaml`, not code):

| Dimension | Weight | Source |
|---|---|---|
| Reversibility | 0.35 | policy lookup per tool (read=0 … hard delete=90) |
| Data scope | 0.25 | log-scale on affected record count |
| Regulatory | 0.20 | policy lookup (PII/financial tags) |
| Model confidence | 0.20 | the LLM's own structured self-assessment, `(1-conf)*100` |

Thresholds: `< 30` autonomous, `30–65` confirm, `> 65` review.

**Hard overrides come first.** Rules in `policy.yaml` (bulk delete > 100
records, email to external domain, full data export) route deterministically
and *bypass scoring entirely*. The scorer advises; the policy rules. A
probabilistic score can never overrule an explicit governance rule.

**Adaptive calibration (bonus — full spec).** Reviewers have three actions:
**approve** (execute as proposed), **reject** (block), and **modify** (execute
with human-edited parameters — the edit replaces the agent's proposal). If
humans cleanly approve a CONFIRM-tier action type ≥ 90% of the time (min 10
samples), its risk drifts down −10 toward autonomous; if they reject **or
modify** it ≥ 40% of the time, it drifts up +15 — a modification means the
agent's proposals for that action type aren't trustworthy as-is. Capped at
±20 and always subordinate to hard overrides — calibration tunes the gray
zone, never the red lines.

## Governed sample workload

A support-ops agent (Amazon Bedrock, Claude, Converse API tool-calling) over a
seeded mock CRM (400 customers). Every tool schema carries a required
`confidence` field the model must fill — that self-assessment feeds the
scorer. Every proposed call goes through `/evaluate` *before* execution; held
actions execute only when a human approves the ticket.

Agent modes (`AUTONOMYGATE_AGENT`): `bedrock` (production) or `scripted`
(deterministic offline planner — used by tests, so the governance path is
provable without any LLM dependency).

## API

| Endpoint | Purpose |
|---|---|
| `POST /evaluate` | Score + route one proposed action (usable by ANY agent, not just the sample) |
| `POST /agent/task` | Give the governed sample agent a natural-language task |
| `GET /queue` · `POST /tickets/{id}/decision` | Pending approvals; approve/reject (approve executes the held action) |
| `GET /audit` | Query the audit log by session/agent |
| `GET /calibration/{action_type}` | Inspect calibration stats + current adjustment |
| `GET /health` | Storage + agent-mode health |
| `GET /` | Live dashboard (task runner, approval queue, audit feed) |
| `GET /docs` | OpenAPI docs (auto-generated) |

## Run locally (no AWS needed)

```bash
pip install -r requirements.txt
pytest              # 15 tests incl. every PS-9.1 success criterion
uvicorn app.main:app --port 8080
# open http://localhost:8080  — scripted agent + SQLite by default
```

## Deploy to AWS (App Runner + DynamoDB + Bedrock)

```bash
# 1. Create tables
AWS_REGION=ap-south-1 python scripts/create_tables.py

# 2. Build & push image
docker build -t autonomygate .
aws ecr create-repository --repository-name autonomygate
docker tag autonomygate:latest <acct>.dkr.ecr.<region>.amazonaws.com/autonomygate:latest
docker push <acct>.dkr.ecr.<region>.amazonaws.com/autonomygate:latest

# 3. App Runner service (console or CLI): port 8080, health check /health,
#    instance role with DynamoDB (the 3 autonomygate-* tables) +
#    bedrock:InvokeModel permissions.
#    Env: AUTONOMYGATE_STORAGE=dynamo  AUTONOMYGATE_AGENT=bedrock
#         AWS_REGION=<region>  AUTONOMYGATE_BEDROCK_MODEL=<claude model id>
```

## Production deployment (what is actually running)

| Layer | Service | Notes |
|---|---|---|
| Public API | **Amazon API Gateway** (HTTP API) | fronts the engine; Function URLs are restricted on new AWS accounts, so the standard API Gateway + Lambda pattern is used |
| Compute | **AWS Lambda** (Python 3.12, 1 GB, 120 s) | FastAPI via Mangum; scales concurrently per request |
| State | **Amazon DynamoDB** (3 on-demand tables) | audit log (append-only), tickets, calibration |
| Identity | **AWS IAM** | dedicated execution role: DynamoDB + Bedrock + CloudWatch logs only |
| Logs | **Amazon CloudWatch** | structured JSON events from every evaluation and decision |
| LLM | **Groq** (self-healing model selection + 429 backoff) | `AUTONOMYGATE_AGENT=bedrock` switches to Amazon Bedrock (Nova/Claude) — one env var; this account's Bedrock invocation is pending AWS's new-account verification (support case filed) |

Deployment is scripted end to end: `python scripts/deploy_lambda.py` builds the
linux wheel package, creates/updates the role, function, and public API. No
Docker required.

## Design decisions (and their tradeoffs)

- **Deterministic scorer, not LLM-as-judge.** Explainable, testable, free,
  fast; the LLM contributes exactly one bounded input. Using an LLM to govern
  an LLM would be circular governance.
- **Approval executes server-side.** The held action's parameters are frozen
  on the ticket at evaluation time — what the human approves is exactly what
  runs; the agent cannot swap parameters after approval (TOCTOU protection).
- **PII is redacted before persistence**, not at display time — raw PII never
  reaches storage.
- **Known limitations** (deliberate scope): no authn on the review endpoints
  (production would add Cognito/OIDC), calibration is per-action-type rather
  than per-parameter-pattern, and the CRM is an in-memory mock.
```

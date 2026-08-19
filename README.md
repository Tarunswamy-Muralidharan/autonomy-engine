# AutonomyGate — Graduated Autonomy Engine

**🔴 LIVE ON AWS:** https://qg37onlhnh.execute-api.us-east-1.amazonaws.com
API Gateway → Lambda → DynamoDB (us-east-1) · dashboard at `/` · OpenAPI at `/docs` · health at `/health`

**Aivar Agentic AI Task — PS-9.1.** A deployed governance engine that risk-scores
every action an AI agent attempts and routes it to the right autonomy level —
**autonomous execution**, **user confirmation**, or **human review** — with a
persisted audit trail and adaptive threshold calibration.

```
Any agent (sample agent, LangChain, CrewAI, your own loop)
        |
        |  proposed tool call
        v
+---------------------------+
|        AutonomyGate       |
|  hard overrides  (policy) |   <- deterministic red lines, checked FIRST
|  4-dimension risk score   |   <- advisory, bounded
|  calibration (asymmetric) |   <- trust may open, never rescue
+------------+--------------+
             |
   +---------+---------+-------------------+
   v                   v                   v
AUTONOMOUS          CONFIRM              REVIEW
executes now    preview + user OK    human review queue
   |                   |                   |
   +---------+---------+-------------------+
             v
   append-only, PII-redacted AUDIT LOG (DynamoDB)
```

---

## How risk is scored

Each proposed action gets a 0–100 score — a weighted sum of four dimensions.
Weights, thresholds, and rules live in `app/engine/policy.yaml`, not in code, so
a risk owner can change governance without touching Python.

| Dimension | Weight | Source |
|---|---|---|
| Reversibility | 0.35 | policy lookup per tool (read = 0 … hard delete = 90) |
| Data scope | 0.25 | log-scale on the **observed** blast radius |
| Regulatory | 0.20 | policy lookup (PII / financial tags) |
| Model confidence | 0.20 | the LLM's own self-assessment, `(1 − conf) × 100`, **floored** |

Thresholds: `< 30` autonomous · `30–65` confirm · `> 65` review.

**Hard overrides come first.** Rules in `policy.yaml` — bulk delete > 100
records, any external email recipient, full or large data export, bulk update —
route deterministically and *bypass scoring entirely*. **The scorer advises; the
policy rules.** A probabilistic score can never overrule an explicit governance
rule, and the policy file is validated at startup so a typo can't silently
disable a red line.

Three properties exist specifically because adversarial testing broke their
absence (see [Security](#security--adversarial-testing)):

- **Blast radius is derived, not trusted.** The engine inspects the payload and
  governs on `max(declared, observed)`, so an agent cannot claim it is touching
  one record while passing five thousand ids.
- **Model confidence is floored.** A self-reported `1.0` cannot, on its own,
  move an action into a lower tier.
- **Calibration is asymmetric.** Earned trust may open the autonomous door; it
  may never pull an action out of human review.

**Adaptive calibration (bonus — full spec).** Reviewers have three actions:
**approve** (execute as proposed), **reject** (block), and **modify** (execute
with human-edited parameters). If humans cleanly approve a CONFIRM-tier action
type ≥ 90% of the time (minimum 10 decisions), its risk drifts down −10 toward
autonomous; if they reject **or modify** it ≥ 40% of the time it drifts up +15 —
a modification means that action type's proposals aren't trustworthy as-is.
Clamped at ±20, subordinate to hard overrides, and reproducible from the audit
trail: calibration tunes the gray zone, never the red lines.

---

## Governing *any* agent

The engine is a standalone policy decision point. `POST /evaluate` is
framework-agnostic — the bundled sample agent is just one caller.

`examples/govern_any_agent.py` adds governance to any tool function in any
framework with a decorator:

```python
@governed(tool="send_slack_message", affected=lambda p: 1)
def send_slack_message(channel: str, text: str):
    ...
```

- **AUTONOMOUS** → your function runs immediately.
- **CONFIRM / REVIEW** → `GovernanceHold` is raised with the ticket id; a human
  decides in the dashboard, then your harness calls `execute_if_approved()`.

Tools the engine doesn't host are **never executed by the engine**. On approval
it records `approved_pending_external_execution` and hands control back — the
calling stack executes and reports the outcome. Verified end-to-end against the
live deployment with a tool the engine had never seen.

---

## Governed sample workload

A support-ops agent over a seeded mock CRM (400 customers). Every tool schema
carries a required `confidence` field the model must fill; every proposed call
goes through the engine *before* execution; held actions execute only after a
human decision.

`AUTONOMYGATE_AGENT` selects the planner:

| Mode | Model | Purpose |
|---|---|---|
| `groq` | Llama via Groq (OpenAI-compatible) | **currently live**; self-heals when a model is retired (404 → live catalog) and backs off on 429 |
| `bedrock` | Amazon Bedrock (Nova / Claude, Converse API) | implemented, one env var away; not invocable on this account (see *AWS free-plan limits* below) |
| `scripted` | deterministic keyword planner | offline; lets the test suite prove the governance path with no LLM dependency |

---

## API

The **proposal plane is open** (any agent may ask for a decision). The **review
plane is authenticated** — a caller must not be able to approve its own action.

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /evaluate` | open | Score + route one proposed action |
| `POST /agent/task` | open | Give the sample agent a natural-language task |
| `GET /queue` | **reviewer** | Pending approvals |
| `GET /tickets/{id}` · `POST /tickets/{id}/decision` | **reviewer** | approve / reject / modify |
| `POST /audit/{action_id}/outcome` | **reviewer** | Report an externally-executed outcome (write-once) |
| `GET /audit` | open | Query the audit log by session / agent |
| `GET /calibration/{action_type}` | open | Calibration stats + current adjustment |
| `GET /health` · `GET /` · `GET /docs` | open | Health, dashboard, OpenAPI |

Reviewer auth is a bearer token (`AUTONOMYGATE_REVIEWER_TOKEN`, generated at
deploy). The dashboard has a sign-in box. In production this becomes an API
Gateway JWT authorizer with per-reviewer Cognito identities — the token is the
minimum viable control that closes self-approval today.

---

## Run locally (no AWS, no keys)

```bash
pip install -r requirements.txt
python -m pytest            # 58 tests
uvicorn app.main:app --port 8080
# http://localhost:8080  — scripted agent + SQLite, review plane open in dev
```

## Deploy to AWS

```bash
python scripts/create_tables.py     # 3 on-demand DynamoDB tables
python scripts/deploy_lambda.py     # role + function + public API, no Docker
```

The deploy script builds a linux wheel package, provisions a **least-privilege**
role, reconciles configuration on every redeploy (preserving secrets), and
generates a reviewer token on first deploy.

### What is actually running

| Layer | Service | Notes |
|---|---|---|
| Public API | **Amazon API Gateway** (HTTP API) | Lambda Function URLs are restricted on new AWS accounts, so the standard API Gateway + Lambda pattern is used |
| Compute | **AWS Lambda** (Python 3.12, 1 GB, 120 s) | FastAPI via Mangum; concurrent instances per request |
| State | **Amazon DynamoDB** (3 on-demand tables) | audit (append-only, conditional writes), tickets, calibration |
| PII detection | **Amazon Comprehend** | contextual NER over audit text — catches names, addresses and national IDs that regex cannot |
| Identity | **AWS IAM** | inline policy scoped to the three `autonomygate-*` tables, `bedrock:InvokeModel`, and read-only Comprehend |
| Logs | **Amazon CloudWatch** | structured JSON for every evaluation and decision |
| LLM | **Groq** → Bedrock-ready | one env var switches providers |

### Layered PII detection (Amazon Comprehend)

Regex catches PII that has **shape** — an email has an `@`, a card has 13–19
digits. It is structurally blind to PII that only has **meaning**: a person's
name, a street address, a passport number in a free-text field. Those are
exactly what leaks into an agent's tool parameters, and exactly what a
regulator asks about.

`AUTONOMYGATE_PII=comprehend` adds `comprehend:DetectPiiEntities` as a second
detector. Both layers produce character spans over the *same* original string;
the spans are merged (longest-wins on overlap) and applied right-to-left in a
single pass, so neither layer can corrupt the other's offsets or nest a marker
inside a marker.

It is designed to fail **safely, not open**:

- regex always runs, so redaction never gets *weaker* than the offline baseline;
- the call is bounded to 4.5 KB and skipped for trivial strings — we cap our own
  latency rather than let AWS reject the request;
- 1 s connect / 2 s read timeout, no retries;
- a **circuit breaker** opens after 3 consecutive failures, so a Comprehend
  outage cannot add its timeout to every `/evaluate` call;
- low-confidence (< 0.85) findings are ignored, and `DATE_TIME` / `URL` /
  `USERNAME` are deliberately **not** redacted — an audit trail an auditor
  cannot read is not safer, just useless.

Only identity- and finance-bearing types are destroyed, including the
India-specific `IN_AADHAAR`, `IN_PAN`, `IN_NREGA` and `IN_VOTER_NUMBER`.
`tests/test_comprehend_redaction.py` covers the failure paths, not just the
happy path — the breaker, the fallback, the bounds, and the offset merge.

### AWS free-plan limits (honest note)

This account is on the AWS free plan, which returns
`ValidationException: Operation not allowed` for **every** Bedrock model —
including Amazon's own Nova, so it is a plan restriction, not a model-access
gate. The Bedrock planner is implemented and selected by one env var; it is not
invocable here. Comprehend is not plan-restricted, which is why the AWS AI
surface in this project is Comprehend rather than Bedrock. If Comprehend were
also unavailable, the circuit breaker degrades the system to regex redaction
with no code change.

---

## Security & adversarial testing

The system was audited by five independent reviewers — two black-box (live API,
no source access) and three white-box (engine logic, storage/concurrency,
AppSec). **Every defect below was proven by execution before being fixed**, and
each now has a regression test in `tests/test_adversarial.py`.

### Governance bypasses found and closed

| Bypass | What it achieved | Fix |
|---|---|---|
| **Multi-recipient email** | `attacker@evil.com, victim@ourcorp.com` passed the "is internal" test. Chained with calibration earned from routine internal mail, the external email was sent **fully autonomously** | every recipient validated independently; unparseable values fail closed |
| **Understated blast radius** | `affected_count: 1` while passing 5 000 ids dodged the bulk-delete override *and* showed the reviewer "1 record" | scope derived from the payload; governs on `max(declared, observed)` |
| **`modify` escalation** | a 3-record delete could be approved with edited params containing 5 000 ids — overrides never re-checked | edited params are re-governed; refused if they escalate beyond the ticket's tier |
| **Self-approval** | the proposing agent approved its own REVIEW ticket | reviewer token + separation of duties (403) |
| **Tool-name evasion** | `DB_DELETE`, `" db_delete"`, unicode variants missed the overrides | names normalized (NFKC, case, whitespace) before matching |
| **Confidence lever** | a self-reported `1.0` flipped CONFIRM → AUTONOMOUS | confidence contribution floored |
| **Calibration demotion** | earned trust downgraded 100-record deletes from REVIEW to one-click CONFIRM | calibration made asymmetric and clamped at the boundary |
| **Attribute dispatch** | a proposed "tool" named `__init__` was routed AUTONOMOUS and, on execution, silently reset the CRM | allowlist enforced before dispatch |
| **Policy typo** | a malformed override rule silently never fired (fail-**open**) | policy validated at startup; the service refuses to boot on an invalid file |

### Fail-open crashes

Six input classes produced a 500 on `/evaluate` — and a 500 there meant the
action was **never scored, never routed, never audited**, the worst failure mode
for a governance gate. Empty and oversized key fields, non-finite floats
(including inside the 422 renderer itself), deep nesting, and out-of-range
integers now return explicit 422s.

### Production-only defects (green tests, broken production)

Tests run on SQLite; production runs on DynamoDB. A parity audit found defects
no passing test could have caught:

- Outcome reporting resolved actions by scanning a 1 000-record window — it
  would have **404'd permanently** once the audit log grew (it was at 221).
  Now a keyed lookup.
- Whole numbers returned as floats, so record id `1000` came back as `1000.0`:
  the engine could approve action A and hand back action A′. Now preserved.
- `agent_id` was silently ignored when combined with `session_id` — an auditor
  filtering to one agent saw everyone's actions, presented as filtered.
- Queue and audit reads never paginated past DynamoDB's 1 MB page limit; at
  ~160 pending tickets, **pending REVIEW items would silently disappear** from
  the reviewer's queue. Now paginated, with server-side filtering.
- Eventually-consistent reads on the decision path; audit writes that could
  overwrite history. Now consistent reads, conditional write-once outcomes, and
  append-only audit puts.

### Controls verified as correct

Double-decision protection under real concurrency (16/16 exactly one 200 + one
409, one execution each); conditional-update atomicity on DynamoDB; no
dashboard XSS (escaping traced through every sink, payload round-trip tested);
no SQL injection (the one dynamic column name is a closed three-layer mapping);
no path traversal or SSRF; **no committed secrets** (full `git log -p` scan);
ReDoS mitigated (worst case 0.03 s per 20 KB); 405s with correct `Allow`
headers on 33 method/path combinations; PII redaction verified live.

---

## Design decisions and tradeoffs

- **Deterministic scorer, not LLM-as-judge.** Explainable, testable, free, and
  fast; the model contributes exactly one bounded input. Using an LLM to govern
  an LLM would be circular governance.
- **Approval executes server-side with frozen parameters.** For `approve`, what
  the human sees is exactly what runs — the agent cannot swap parameters after
  approval (TOCTOU protection). For `modify`, the reviewer's edit is a *new
  proposal* and is re-governed before it can execute.
- **Fail closed, everywhere.** A missing parameter, an unparseable value, an
  unknown tool, an invalid policy file, or an unrecognized payload shape all
  route toward *more* human oversight, never less.
- **Audit redaction is honest about its boundary.** Audit records are redacted
  before persistence — including dict keys and numeric values (a 16-digit card
  sent as a JSON number was previously stored in the clear). Ticket `params` are
  stored **raw and deliberately**: approval executes them, and redacted
  parameters would execute the wrong action. Ticket previews are redacted and
  length-capped. The tickets table is therefore the sensitive store and is
  behind reviewer auth.

## Known limitations (deliberate, and what production would add)

- **Reviewer auth is a shared bearer token**, not per-reviewer identity. It
  closes self-approval but cannot attribute a decision to a named human;
  production would use an API Gateway JWT authorizer + Cognito and derive
  `decided_by` from the verified token rather than the request body.
- **No rate limiting.** An API Gateway usage plan is the natural next step
  (cost and write amplification are otherwise unbounded).
- **The CRM is an in-memory mock** (as the problem statement specifies), so it
  resets per Lambda instance — governance state is in DynamoDB and is not
  affected.
- **Calibration is per action type**, not per parameter pattern, and is not
  scoped per tenant.
- **Bedrock is not invocable on this account** — the AWS free plan rejects every
  model, Amazon's own included. The planner is implemented and one env var away;
  Amazon Comprehend carries the AWS AI surface instead.
- **Comprehend runs synchronously in the request path.** At higher volume the
  right shape is to redact asynchronously off a DynamoDB stream, or batch with
  `BatchDetectPiiEntities`, so audit writes never wait on an NER call.

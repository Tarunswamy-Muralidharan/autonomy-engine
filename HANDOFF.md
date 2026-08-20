# AutonomyGate — Handoff

Written 2026-08-20 for whoever picks this up next (human or another AI
assistant). Everything below is verified state, not intention.

---

## 1. The two things that matter first

**`main` is the submission and it is DONE.** It is deployed, spec-complete,
and must not be destabilised. If you change one thing on `main`, re-run the
tests and re-verify the live URL.

**`v2-production` is a separate branch.** It has never been deployed. It is
where all the hardening work lives. Merging it to `main` is a decision, not a
formality — see §6.

---

## 2. Project context

- **What it is:** AutonomyGate, a governance gate for AI-agent actions. Every
  proposed action is risk-scored on four dimensions and routed to
  AUTONOMOUS / CONFIRM / REVIEW, with an append-only PII-redacted audit trail
  and adaptive calibration.
- **Why it exists:** Aivar Innovations campus-hiring task round, problem
  statement PS-9.1. Graded on how industry-ready the project is and how AWS
  services are blended in.
- **Owner:** Tarunswamy Muralidharan, PSG iTech. Absolute beginner in
  backend/JS; explain things at that level, patiently. He does the AWS
  console and credential steps himself — never handle his API keys, and never
  paste secrets into chat or files.
- **Deadline:** the task-round submission was due 2026-08-20 17:00 IST.
  Confirm current status with him before assuming anything is still pending.

## 3. Live production system (from `main`)

| Item | Value |
|---|---|
| Live URL | `https://qg37onlhnh.execute-api.us-east-1.amazonaws.com` |
| Stack | API Gateway HTTP API → Lambda (Python 3.12, Mangum/FastAPI) → DynamoDB |
| AWS account | 055903697646, us-east-1, IAM user `autonomygate-dev` |
| AWS CLI | `C:\Program Files\Amazon\AWSCLIV2\aws.exe` |
| Agent LLM | Groq (`AUTONOMYGATE_AGENT=groq`) |
| PII detection | Amazon Comprehend (`AUTONOMYGATE_PII=comprehend`), live-verified |
| Deploy | `python scripts/deploy_lambda.py` (no Docker) |
| Tables | `python scripts/create_tables.py` |
| Repo | `Tarunswamy-Muralidharan/autonomy-engine` (private) |

**Bedrock is blocked** by the AWS free plan — every model, including Amazon's
own Nova, returns `ValidationException: Operation not allowed`. The Bedrock
planner is written and one env var away. The "Upgrade plan" button glitches
(bounces to Console Home) on this account. Comprehend is NOT plan-gated, which
is why it carries the AWS-AI story.

**Secrets** live only in the Lambda environment (`GROQ_API_KEY`,
`AUTONOMYGATE_REVIEWER_TOKEN`). Read the reviewer token with:

```
& "C:\Program Files\Amazon\AWSCLIV2\aws.exe" lambda get-function-configuration `
  --function-name autonomygate --region us-east-1 `
  --query "Environment.Variables.AUTONOMYGATE_REVIEWER_TOKEN" --output text
```

## 4. Branch state

### `main` — commit `94fb5fd`
- 63 tests passing, deployed, verified live.
- Last change (Phase 0): closed a real disclosure chain found in external
  review — `GET /audit` is now reviewer-only, and `GET /tickets/{id}` returns
  a status-only view to unauthenticated callers (raw params are
  reviewer-only). Regression tests in `tests/test_disclosure.py`.
- Also carries `ROADMAP.md`, the dependency-ordered production plan.

### `v2-production` — commit `37f4a74`
- **197 passing, 9 skipped**, never deployed.
- Seven modules built (in parallel by subagents), then wired into the engine,
  then audited.

| Module | Purpose |
|---|---|
| `app/identity/` | hashed agent API keys, per-reviewer tokens, tenant seam |
| `app/policystore/` | versioned immutable policies, 60s cache, verdict stamping |
| `app/contracts/` | per-tool manifests: score the ACTION (field sensitivity) |
| `app/cumulative/` | session/day ledger — closes salami-slicing; velocity guard |
| `app/capability/` | signed, expiring, single-use execution capabilities (KMS/HMAC) |
| `app/humanplane/` | ranked+deduped queue, SLA expiry-as-denial, kill switch |
| `app/auditchain/` | content hashes, Merkle anchors, WORM export design |
| `app/engine/calibration_v2.py` + `app/calibrationstore/` | context-keyed trust with decay (built, **NOT yet integrated**) |

Every module has `docs/integration/<name>.md` with the exact wiring diffs.
Read those before touching anything.

## 5. Where the work stopped (pick up here)

Ordered by priority. Items 1–3 were in flight when work paused.

1. **Integrate calibration v2.** Module + 26 tests are green and committed;
   the wiring is not applied. Follow `docs/integration/calibration_v2.md`.
   It is env-gated (`AUTONOMYGATE_CALIBRATION=v2`, default v1) so integration
   is zero-regression by design.

2. **Wire real multi-tenancy.** `tenant_id` is threaded through every module
   and storage key but pinned to `"default"` in
   `app/engine/service.py:run_evaluation`. Derive it from
   `principal.tenant_id` (identity already carries it) and thread it into the
   ledger, kill switch, policy resolution and calibration context.
   `tests/test_tenancy.py` exists and auto-skips until the seam is wired —
   those 9 skips turn green when you finish. A partial
   `docs/integration/tenancy.md` may exist.

3. **Property + fuzz tests.** `tests/test_properties.py` was landing when work
   stopped — verify it runs, and add `tests/test_fuzz.py`: the invariant is
   that no request to any endpoint ever yields a 500 (only 401/403/404/409/
   413/422), because a 500 on this system means an action was never scored,
   routed or audited.

4. **Black-box audit.** Not yet run. Start a local instance and attack it:
   self-approval, tool-name evasion, salami-slicing across sessions,
   capability replay/tamper, cross-tenant reads, queue flooding. Fix findings,
   add regression tests.

5. **Deferred white-box findings.** #7 (a permissive hard override would
   suppress cumulative/velocity checks — no live impact today because every
   shipped override routes to REVIEW; `_ROUTE_RANK` scaffolding exists in
   `service.py` for a strictest-wins resolution) and #9 (LOW: a mistyped
   manifest `param` under-counts; fails closed to legacy behaviour).

6. **Never built, deliberately:** the business-moat items from the review
   (§7) — that is strategy, not code.

## 6. If asked to merge v2 into main

Do not do it casually. The considerations:

- v2 has **never been deployed**. Going live means creating six new DynamoDB
  tables, redeploying, and re-verifying the whole surface.
- The README, the two PDFs, and the demo script all describe `main`'s
  architecture. A merge makes them wrong.
- The submission is graded on spec-match and clarity, not feature count.

The safe path already taken: `main` stays deployed; the README/ROADMAP point
at `v2-production` so a reviewer can see the hardening work without any
deployment risk.

## 7. Study material on the Desktop

| File | What it is |
|---|---|
| `AutonomyGate_Deep_Dive.pdf` | 36pp — v1, every file explained, 11 red-team attack stories, glossary, 20 Q&As |
| `AutonomyGate_V2_Handbook.pdf` | 16pp — v2: the CTO critique, all modules, the white-box audit war stories, the honest-gaps chapter, interview Q&As |
| `Aivar_MCQ_Master_Prep.pdf`, `Aivar_OA_Hotlist.pdf` | MCQ round (already cleared) |
| `Agentic_AI_Master_Visual_Guide.pdf` | agentic-AI course notes |

Remaining deliverable for the round: **the demo video** — script at
`demo/demo_script.md` (5–5.5 min, includes a Comprehend segment). He records
it himself; warm the Lambda first to avoid a cold start on camera.

## 8. Working agreements with this user

- He is a beginner: explain concepts before code, one layer at a time, and
  check understanding rather than dumping.
- He asks for PDFs as study material — build them with reportlab + matplotlib
  (the working pipeline is in the session scratchpad: `build_pdf.py`,
  `pdf_diagrams*.py`, `content_*.py`).
- **Never** handle his API keys, even when he offers. The working pattern is:
  write a command file with a `PASTE_HERE` placeholder, he fills and runs it
  himself, then delete the file.
- PowerShell 5.1 on Windows: no `&&`, no ternary; commit messages with
  apostrophes or `@` break here-strings — use `git commit -F <file>`.
- Be honest about what is and is not done. He explicitly asked for the harsh
  external review to be taken seriously rather than defended against, and
  that instinct has been the most valuable thing in this project.

## 9. The one-line summary of the whole system

Trust is earned, bounded, and verified — never assumed. Every design decision
in v1 and v2 re-derives from that sentence.

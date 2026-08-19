# AutonomyGate — Demo Video Script (target 5–5.5 minutes)

Record with OBS or Windows Game Bar (Win+G). Screen + mic. One 1080p monitor.
Have open in tabs BEFORE recording: the live dashboard, /docs, the GitHub repo,
AWS console (Lambda + DynamoDB pages), and a terminal.

Rate-limit note: Groq free tier throttles per minute. Run each agent task,
then talk for ~30s before the next one — the pacing below is designed for it.

---

## 0:00 — Cold open on the live dashboard (30s)

URL: https://qg37onlhnh.execute-api.us-east-1.amazonaws.com

SAY: "This is AutonomyGate, my solution to PS-9.1 — a graduated autonomy
engine, deployed live on AWS. Every action an AI agent tries to take is
risk-scored across four dimensions and routed one of three ways: low risk
executes autonomously, medium risk waits for user confirmation, high risk
goes to a human review queue. Let me show you all three, live."

## 0:30 — Task 1: autonomous path (45s)

In the dashboard task box: `What is the email of customer 1007?`

SAY while it runs: "The agent is a real LLM served by Groq, with a mock CRM
of 400 customers. Watch the audit feed — the model proposed a crm_read, the
engine scored it about 15 out of 100 — reads are reversible, single record,
high model confidence — so it executed autonomously. No human needed for
safe actions; that's the point of GRADUATED autonomy."

## 1:15 — Task 2: confirmation path (60s)

Task box: `Update customer 1003's plan to enterprise`

SAY: "An update is different: it changes data. Risk lands in the middle band
— about 33 — so nothing executed. It's waiting in the approval panel with a
preview of exactly what will change." APPROVE it on the dashboard.
"On approval, the engine executes the action server-side with the parameters
frozen at evaluation time — the agent can't swap them after I approve. Now
watch the audit log record both the decision and the outcome."

## 2:15 — Task 3: review path + hard override (60s)

Task box: `Delete all inactive customers from the database`

SAY: "Now something dangerous. The model first searched for inactive
customers — reads, autonomous — then proposed a bulk delete. Deletes are
irreversible, and my policy file has a hard override: bulk deletion above
100 records ALWAYS goes to human review, no matter what the score says.
The scorer advises; the policy rules. A probabilistic model can never
overrule an explicit governance rule." REJECT it with a note.
"Denied — and the agent was told it cannot proceed. Nothing was deleted."

## 3:15 — The AWS architecture (45s)

Switch to AWS console tabs: Lambda function page, then DynamoDB tables.

SAY: "Everything you just saw runs on AWS: API Gateway in front, the engine
on Lambda — it scales per-request — and every audit record, ticket, and
calibration stat persists in DynamoDB. Logging goes to CloudWatch, and the
whole deployment is one script — no Docker."

## 3:45 — Amazon Comprehend doing real work (45s)

Scroll the dashboard's audit feed to the record from Task 2, or re-run:
`Add a note to customer 1003: escalate the refund for Priya Raman at 14 Peters Road, Chennai`

SAY: "There's a second AWS AI service in here, and it's solving a real
problem. Audit logs are themselves sensitive — agent parameters carry
customer data — so I redact before persisting. Regex handles PII that has a
SHAPE: an email has an at-sign, a card has sixteen digits. But regex is
structurally blind to PII that only has MEANING — a person's name, a street
address. Look at the stored record: the name and the address are both gone.
That's Amazon Comprehend doing named-entity recognition on the audit text.

And it's layered, not swapped — regex always runs, so redaction can never get
weaker than the offline baseline. Comprehend is bounded to a two-second
timeout with a circuit breaker: three failures and it stops calling out
entirely and falls back to regex. A PII detector that can take down the
governance gate is worse than no PII detector at all.

I'd originally built this on Bedrock too — the planner's in the repo, one env
var away — but this account is on the AWS free plan, which rejects every
Bedrock model including Amazon's own Nova. So I moved the AWS AI surface to
Comprehend, where it does something the system genuinely needed."

## 4:30 — Code + tests + bonus (45s)

Switch to GitHub repo. Scroll: policy.yaml, tests folder, README table.

SAY: "The governance is declarative — weights, thresholds, and hard
overrides live in a policy file, not code. Fifty-eight automated tests cover
every success criterion in the problem statement, plus a regression suite for
every bypass I found red-teaming my own system — I ran black-box and
white-box audits against it and fixed what they proved. And the bonus:
adaptive calibration — if
humans keep approving an action type, its risk drifts down toward
autonomous; consistent rejections push it up. Capped, and always subordinate
to the hard overrides — the system learns, but the red lines don't move."

## 5:15 — Close (15s)

Back on the dashboard.

SAY: "Deployed on AWS, real LLM, full audit trail, human oversight where it
matters and autonomy where it doesn't. That's AutonomyGate. Thanks."

---

## Pre-flight checklist (run 10 minutes before recording)

- [ ] `GET /health` returns healthy
- [ ] Run one throwaway agent task to warm the Lambda (avoids cold-start lag on camera)
- [ ] Dashboard queue is EMPTY (approve/reject leftovers from testing)
- [ ] AWS console logged in, on the Lambda function page + DynamoDB tables tab
- [ ] GitHub repo tab open at the README
- [ ] Close every unrelated tab/window; mute notifications (Win+N → focus assist)
- [ ] Timer visible to yourself; if a Groq 429 stalls a task on camera, keep
      talking — the engine retries automatically and it recovers in ~20s

## Submission package

1. Live URL: https://qg37onlhnh.execute-api.us-east-1.amazonaws.com
2. GitHub repo link (add reviewers as collaborators if they ask, or flip to public)
3. Demo video (upload unlisted YouTube or Drive link)
4. One-paragraph summary (README's opening section works verbatim)

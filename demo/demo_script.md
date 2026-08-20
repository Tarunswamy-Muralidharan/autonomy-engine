# AutonomyGate — Demo Video Script (target 6–7 minutes)

Slide-driven, with the live system as the centrepiece.

**Open before recording:**
- `Desktop\AutonomyGate_Diagrams.pptx` — press **F5** for fullscreen, arrows to move
- Live dashboard: https://qg37onlhnh.execute-api.us-east-1.amazonaws.com
- AWS console: Lambda page, DynamoDB tables page
- GitHub: https://github.com/Tarunswamy-Muralidharan/autonomy-engine

**How to switch:** `Alt+Tab` between the slide show and Chrome. Practise the
switch twice before you record — it is the only fiddly bit.

**Rate-limit note:** Groq's free tier throttles per minute. Each agent task is
followed by ~30s of talking, which is exactly the spacing you need.

**Slide map** (deck = `AutonomyGate_Diagrams.pptx`)

| Slide | When |
|---|---|
| 1 cover | 0:00 cold open |
| 2 architecture | 3:40 AWS segment |
| 3 life of one action | 0:35 before the demos |
| 4 log scale | optional, skip if tight |
| 5 thresholds + calibration | 5:10 bonus feature |
| 6 ticket lifecycle | optional |
| 7 circuit breaker | 4:20 Comprehend |
| 8 V2 pipeline | 5:50 what's next |
| 9 V2 salami | 6:05 |
| 10 V2 capabilities | 6:20 |
| 13 closing line | 6:45 |

---

## 0:00 — Cold open  ▸ SLIDE 1 (cover)

SAY: "This is AutonomyGate — my solution to problem statement 9.1. It's a
graduated autonomy engine for AI agents, and it's deployed live on AWS.

The problem it solves is simple to state. Companies want AI agents that can
actually *do* things — read records, send emails, delete data. But full trust
means one bad action is irreversible, and zero trust means a human approves
five hundred things a day, stops reading, and rubber-stamps. Both extremes
fail.

So instead of trusting an agent completely or not at all, this system grants
autonomy in *degrees* — matched to how dangerous each individual action is."

## 0:35 — How it works  ▸ SLIDE 3 (life of one action)

SAY: "Here's what happens to every single action an agent proposes.

The agent describes what it wants to do. The engine scores it across four
dimensions: can this be undone, how many records does it touch, is the data
regulated, and how confident is the model in its own plan. That produces a
score out of a hundred.

Then — and this ordering matters — **hard overrides are checked before the
score is used**. If an explicit rule matches, the score becomes irrelevant.

Below thirty, it executes autonomously. Between thirty and sixty-five, it
pauses for the user to confirm. Above that, a human reviewer must approve it.
And every action is written to an audit trail, including the ones that ran
automatically.

Let me show you all three tiers on the live system."

*(Alt+Tab to Chrome, dashboard)*

## 1:10 — Task 1: autonomous  ▸ LIVE

Type: `What is the email of customer 1007?` — press Enter.

SAY: "The agent here is a real LLM — Llama, served by Groq — working against a
mock CRM of four hundred customers. It chose a read tool.

The engine scored it about sixteen: reads are reversible, it touches one
record, and the model was confident. Below thirty, so it executed immediately
and the result came straight back. No human involved — because nothing about
this action needed one. That's the *graduated* part."

## 2:00 — Task 2: confirmation  ▸ LIVE

Type: `Update customer 1003's plan to enterprise` — Enter.

SAY: "An update is different — it changes data. This scored in the middle
band, so nothing executed. It's waiting in the approval panel with a preview
of exactly what will change."

*(Click Approve)*

"And now the important part: on approval, the engine executes the action
itself, server-side, using the parameters **frozen at the moment it was
scored**. The agent cannot swap the payload between me approving it and it
running. Watch the audit log — the outcome just flipped to executed, and it
records who decided."

## 3:00 — Task 3: the red line  ▸ LIVE

Type: `Delete customer records 1000 through 1200` — Enter.

> **Use this exact phrasing.** Two traps found while rehearsing:
> "delete this entire database" makes the LLM refuse outright — its own
> guardrail, before governance is ever consulted — and you get no steps at
> all. "delete all inactive customers" makes it run a *search* first, which is
> itself held at CONFIRM because it reads 120 PII records, so the run stops
> before the delete. Backup: `Purge customer accounts with ids from 1000 to 1150`

SAY: "Now something genuinely dangerous — a bulk delete, two hundred and one
records.

Look at the audit row: `override: bulk_delete_always_review`. My policy file
says bulk deletion above a hundred records **always** goes to human review, no
matter what the score says. The score here was sixty-two — but that number
never got a vote.

This is the core principle of the whole project: **the scorer advises, the
policy rules**. A probabilistic model can never overrule an explicit business
rule."

*(Click Reject)*

"Denied. Nothing was deleted, and the agent was told it cannot retry."

## 3:40 — The AWS architecture  ▸ SLIDE 2

SAY: "Here's what's actually running.

API Gateway is the public door. The engine runs on Lambda — serverless, so it
scales per request and costs nothing when idle. Every audit record, held
ticket and calibration statistic persists in DynamoDB. Amazon Comprehend does
PII detection, and Groq serves the agent's model.

There's no server to patch and nothing running while idle. The whole
deployment is one script — no Docker."

*(Optional: Alt+Tab to the AWS console, show the Lambda page and DynamoDB
tables for ~10 seconds, then come back.)*

## 4:20 — Comprehend, and failing safely  ▸ SLIDE 7 (circuit breaker)

SAY: "There's a second AWS AI service in here, solving a real problem.

Audit logs are themselves sensitive — agent parameters carry customer data —
so I redact before anything is stored. Regex handles PII that has a *shape*:
an email has an at-sign, a card has sixteen digits. But regex is structurally
blind to PII that only has *meaning* — a person's name, a street address.
That's what Amazon Comprehend catches.

And look at how it's wired. Regex **always** runs, so redaction can never get
weaker than the offline baseline. Comprehend is bounded to a two-second
timeout, and if it fails three times in a row this circuit breaker opens and
the system falls back to regex entirely.

The reasoning: a privacy feature that can take down the governance gate is
worse than no privacy feature at all."

## 5:10 — It learns  ▸ SLIDE 5 (thresholds + calibration)

SAY: "This is the bonus requirement, and it's live.

If humans consistently approve an action type without modifying it, its risk
drifts *down* toward autonomous. If they reject or modify it, risk drifts
*up*. On the deployed system right now, `db_delete` has twenty-three
approvals, so it carries a minus-ten adjustment — you can see it in the audit
rows.

But look at the asymmetry on this diagram. Earned trust may open the
autonomous door — it can **never** pull an action out of human review. And it
can never touch a hard override.

So the system learns, and the red lines don't move."

## 5:50 — Where it goes next  ▸ SLIDES 8, 9, 10

SAY: "I also had this reviewed by a senior engineer who scored it four out of
ten for enterprise readiness. Rather than defend it, I took the critique at
face value and built the answer — it's on the `v2-production` branch.

*(SLIDE 9 — salami)* The sharpest finding: my red line catches one bulk delete
of a hundred records, but an agent could split it into a hundred single-record
deletes, each individually harmless. So I added a per-session ledger — and
this one is **live in production right now**: ten deletes of ten records pass,
and the hundred-and-first record trips the same wire.

*(SLIDE 10 — capabilities)* The second: for tools the engine doesn't own, an
approval was just *advice* — the caller could ask about a Slack message and
delete a database instead. Now approval issues a signed, single-use token
bound to a hash of the exact parameters. Change one byte and the executor
refuses it. Approval becomes enforcement.

*(SLIDE 8 — pipeline)* Alongside those: per-reviewer identity so the audit
says *who* approved, versioned policies so every verdict records which
rulebook judged it, and tamper-evident audit hashes. Two hundred and thirty-
four tests."

## 6:45 — Close  ▸ SLIDE 13

SAY: "What this project brings is governance you can actually adopt. The
rules are declarative — weights, thresholds and red lines live in a policy
file, not in code, so a compliance officer can change them without a
developer. Every decision is explainable and permanently recorded. And I
red-teamed my own system, found eleven working bypasses, and closed every one
with a regression test.

Trust is earned, bounded, and verified — never assumed.

That's AutonomyGate. Thank you."

---

## Pre-flight checklist (10 minutes before recording)

- [ ] Deck open, **F5** pressed once to confirm fullscreen works
- [ ] Practise `Alt+Tab` between deck and Chrome twice
- [ ] Dashboard hard-refreshed (`Ctrl+Shift+R`) and **signed in** with the token
- [ ] `GET /health` returns healthy
- [ ] One throwaway agent task run, to warm the Lambda (avoids a cold-start pause)
- [ ] Approval queue EMPTY
- [ ] AWS console logged in, Lambda + DynamoDB tabs ready
- [ ] Every unrelated tab and window closed; notifications muted
- [ ] Mic tested — record 10 seconds and **play it back** before the real take

## If something goes wrong on camera

- **A task stalls** — keep talking. Groq rate-limits and recovers in ~20s.
- **The LLM refuses** — the dashboard now says so explicitly. Use it: *"that's
  the model's own guardrail firing before my engine is even consulted — two
  different layers."* Then re-run with the scripted phrasing.
- **Anything else** — don't restart. One clean take with a small stumble beats
  five retakes and a missed deadline.

## Submission package

1. Live URL: https://qg37onlhnh.execute-api.us-east-1.amazonaws.com
2. GitHub: https://github.com/Tarunswamy-Muralidharan/autonomy-engine *(public)*
3. Demo video (unlisted YouTube or Drive link)
4. Reviewer token — it is also shown on the dashboard itself, so evaluators
   can exercise the approval queue from the URL alone

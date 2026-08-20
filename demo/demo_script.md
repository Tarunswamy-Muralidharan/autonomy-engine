# AutonomyGate — Demo Video Script (target 9–10 minutes)

Layer-by-layer walkthrough. Three surfaces, rotated deliberately:

- **DECK** — `Desktop\AutonomyGate_Layers.pptx` (F5 for fullscreen)
- **CODE** — Antigravity, repo `C:\Users\tmswa\Code\autonomy-engine`
- **LIVE** — https://qg37onlhnh.execute-api.us-east-1.amazonaws.com

Pattern for every layer: **slide explains it → code proves it → live shows it.**

**Before recording:** open all three, and practise `Alt+Tab` between them
until it's automatic. In Antigravity, pre-open these tabs in this order so you
can jump without searching:

```
app/main.py
app/engine/service.py
app/engine/scorer.py
app/engine/router.py
app/engine/policy.yaml
app/agent/tools.py
```

---

# PART ONE — how it works (0:00 – 5:30)

## 0:00 — Cold open  ▸ DECK slide 1

SAY: "This is AutonomyGate, my solution to problem statement 9.1 — a
graduated autonomy engine for AI agents, deployed live on AWS.

The problem: companies want agents that can actually *do* things. But full
trust means one bad action is unrecoverable, and zero trust means a human
approves five hundred things a day, stops reading, and rubber-stamps. Both
extremes fail.

So this system grants autonomy in *degrees*, matched to how dangerous each
individual action is. I'll walk you through it layer by layer, and show you
the code that implements each one."

## 0:30 — The stack  ▸ DECK slide 2

SAY: "Nine layers. A request enters at the top and either executes, waits
for a user, or waits for a reviewer — and whatever happens, it gets written
down.

One property holds across all nine, and it's the thing that makes a
governance system trustworthy: **every layer fails closed**. On any error —
bad input, storage down, unknown tool, unreadable policy — the system routes
toward *more* oversight, never less. There is no failure mode anywhere in here
that grants more autonomy."

---

## 1:00 — LAYER 1: the API surface  ▸ DECK slide 3 → CODE

SAY: "Layer one. The API is split into two planes with different trust."

▸ **CODE: `app/main.py`** — scroll to `@app.post("/evaluate")`

SAY: "This is the proposal plane — deliberately open. Any agent, in any
framework, must be able to ask 'may I do this?'. Locking it would make the
gate unusable."

▸ scroll to `decide_ticket`, highlight the separation-of-duties check

SAY: "And this is the review plane — locked. It needs a reviewer token,
because red-teaming proved the obvious attack: propose a dangerous action,
read the ticket id out of the response, then approve your own ticket.
Governance the governed party can overrule is decoration.

And even *with* a valid token — this check — the identity that proposed an
action can never be the one that approves it."

## 1:40 — LAYER 2: validation  ▸ DECK slide 4 → CODE

▸ **CODE: `app/engine/service.py`** — `class EvaluateRequest`

SAY: "Layer two looks boring. It isn't.

For most web apps a 500 error is embarrassing. For a governance gate it's a
*security failure* — because if the gate crashes while evaluating an action,
that action was never scored, never routed, and never audited.

So I fuzzed it. NaN in the confidence field, ten-to-the-three-hundred in the
count, dictionaries two hundred levels deep. Every one of those was a crash
once. Every constraint you see here is a crash converted into a clean,
logged, fail-closed rejection."

## 2:15 — LAYER 3: the scorer  ▸ DECK slide 5 → CODE

▸ **CODE: `app/engine/scorer.py`** — `score_action`

SAY: "Layer three is the risk calculation, and the most important thing about
it is what it *isn't*: it is not an LLM call.

Four dimensions — can this be undone, how many records, is the data
regulated, and how sure is the model. Weighted sum, out of a hundred.

Why not ask a model? Because governance must be deterministic — the same
action must score the same six months later when an auditor asks. It must be
explainable, testable, and free, because it runs in front of *every* action.
And decisively: the model is the thing being governed. Letting it score
itself is letting the defendant pass sentence."

▸ highlight `MAX_TRUSTED_CONFIDENCE = 0.9`

SAY: "That's the one input the model controls — its own confidence. So it's
capped. Claiming 100% certainty buys exactly nothing above 0.9. Low
confidence is trusted completely, because volunteering doubt is evidence
against yourself. Asymmetric trust, in one line."

## 3:00 — LAYER 4: the router  ▸ DECK slide 6 → CODE

▸ **CODE: `app/engine/router.py`** — `evaluate_action`

SAY: "Layer four decides. And the *order* here is the entire policy.

Step one: hard overrides. Step two: thresholds. Overrides are checked
**before** the score is used — if an explicit rule matches, the score is
recorded but ignored.

That's the core principle of the whole project: **the scorer advises, the
policy rules.** A probabilistic number can never overrule an explicit
business rule."

▸ **CODE: `app/engine/policy.yaml`**

SAY: "And here are the rules themselves — in a config file, not in code.
Weights, thresholds, red lines. A compliance officer can change 'bulk delete
above 100 records' to 10 without a developer, a code review, or a redeploy.
That's what declarative governance means."

▸ back to `router.py`, highlight `normalize_tool` and `review_score = max(...)`

SAY: "Two details worth pointing at. Tool names are canonicalized before
matching — red-teaming got a bulk delete past the red line four ways:
uppercase, a leading space, a trailing newline, and a unicode hyphen that
looks identical in every font.

And this `max`: earned trust may open the autonomous door, but it can never
pull an action *out* of human review."

## 3:45 — LAYER 5: blast radius  ▸ DECK slide 7 → CODE

▸ **CODE: `app/engine/service.py`** — `observed_blast_radius`

SAY: "Layer five is my favourite catch. The agent reports how many records it
affects — `affected_count`. That's a field the *governed party* fills in about
itself.

Red-teaming sent a delete declaring one record, while passing five thousand
record ids. The declared number is what got scored and what the reviewer's
screen displayed: 'a delete affecting one record'.

So now the engine walks the actual payload, finds the largest collection at
any depth, and governs on the worst of the two. An agent may under-*report* —
it can never under-*govern*. The information the attacker needs to do the
damage is the same information that convicts them."

## 4:20 — LAYERS 6, 7, 8  ▸ DECK slides 8, 9, 10

SAY: "Layer six: a held action becomes a ticket, and approval executes the
parameters **frozen at scoring time** — the agent can't swap the payload
between the human's glance and execution. A ticket can be decided exactly
once, and that's enforced by the database, not by application code, so two
concurrent approvals can't both win.

Layer seven: the tool name is model-supplied, so it's checked against an
allowlist *before* any attribute lookup. Red-teaming proposed a tool named
`__init__` and the original code happily called it.

Layer eight: every evaluation is recorded — redacted, append-only, and
sealed. Two layers of PII detection: regex for PII with a *shape*, and Amazon
Comprehend for PII that only has *meaning* — names, street addresses. The
Comprehend call has a two-second timeout and a circuit breaker, because a
privacy feature that can take down the governance gate is worse than none."

## 5:00 — LAYER 9: calibration, the bonus requirement  ▸ DECK slides 11, 12 → CODE

▸ **CODE: `app/engine/calibration.py`** — the whole file is about 16 lines

SAY: "Layer nine is the bonus requirement, and it's live.

The problem statement asks: if users consistently confirm an action type
*without modification*, lower its risk automatically; if they modify or reject
it, raise it. Note 'without modification' — a human who edits the parameters
before approving isn't expressing trust, they're saying the agent got it
wrong. So modifications count on the *distrust* side of the ledger.

And this is rules, not machine learning — deliberately. A governance system
has to answer 'why is this trusted right now?' in one sentence: *because
humans approved this exact action type twenty-three times without a single
rejection.* A gradient can't give an auditor that sentence. Every number here
is recomputable from the audit trail."

▸ **DECK slide 12** — the four locks

SAY: "Automatic risk reduction is obviously dangerous, so it has four locks.
No adjustment at all until ten human decisions — one enthusiastic approval
can't start loosening anything. Trust needs ninety percent near-unanimity;
distrust triggers at forty. Trust moves it down ten points, distrust moves it
up fifteen — distrust is cheaper to earn and stronger when earned. And the
whole thing is capped at twenty.

Then the asymmetry that matters most: earned trust may open the autonomous
door, but it can **never** pull an action out of human review, and it never
touches a hard override.

**The system learns, and the red lines don't move.** I'll show you that
happening on real data in a moment."

---

# PART TWO — see it run (5:30 – 7:00)  ▸ LIVE

> **This section demonstrates all four PS-9.1 success criteria in order.**
> Task 1 = criterion 3 · Task 2 = criterion 2 · Task 3 = criterion 1 ·
> the audit walkthrough = criterion 4. Don't skip the audit beat — it is a
> graded requirement, not a nice-to-have.

SAY: "That's the architecture. Now the live system on AWS — and this is
exactly the four success criteria from the problem statement, in order."

**Task 1** — type `What is the email of customer 1007?`

SAY: "Real LLM, Llama on Groq, against a mock CRM of 400 customers. Scored
about sixteen — reversible, one record, confident — so it executed
immediately. No human needed for safe actions."

**Task 2** — type `Update customer 1003's plan to enterprise` → **Approve**

SAY: "This one changes data. Middle band, so nothing ran — it's waiting with
a preview. I approve… and the engine executes it server-side with the frozen
parameters. The audit row now shows the outcome and who decided."

**Task 3** — type `Delete customer records 1000 through 1200` → **Reject**

> Use this exact phrasing. "delete this entire database" makes the LLM refuse
> outright — its own guardrail, before governance is consulted. "delete all
> inactive customers" runs a search first, which is itself held.

SAY: "Two hundred and one records. Look at the audit row: `override:
bulk_delete_always_review`. The score was sixty-two — but it never got a vote.
The red line fired first."

*(Reject)* "Denied. Nothing deleted, and the agent was told not to retry."

**Criterion 4 — the audit breakdown** — point at the `crm_update` row from Task 2

SAY: "And the fourth success criterion: the risk breakdown has to be accurate
and human-readable in the audit log. Here it is.

Time, what the action was, the route, and the four dimensions in the open:
reversibility forty, data scope ten, regulatory sixty, confidence twenty,
total thirty-two point five.

And that total isn't a black box — it's the weighted sum you can check
yourself: point-three-five times forty, plus point-two-five times ten, plus
point-two times sixty, plus point-two times twenty. Thirty-two point five.

Plus a plain-English reason — 'in confirmation band' — and who decided it.
Nothing about this decision is unexplainable six months from now."

**Calibration on real data** — point at any `db_delete` row showing `calib: -10`

SAY: "And here's the calibration I described, working live. This `calib:
minus ten` means humans have approved `db_delete` twenty-three times on this
system, so its risk has genuinely drifted down — single deletes now flow more
easily than they did on day one.

But look at the row above it: that two-hundred-record delete still went
straight to REVIEW. The learning is real, and the red line is completely
untouched by it. That's the entire design, visible in two rows."

---

# PART THREE — where I found weaknesses (7:00 – 8:40)

## 7:00 — Auditing my own design  ▸ DECK slide 13

SAY: "Once the core was working and tested, I went back and attacked the
design itself — not looking for bugs this time, but asking where the
*governance model* was too simple to survive a real enterprise.

I found five things I wasn't happy with. Here's the before and after of each."

## 7:15 — Improvement 1  ▸ DECK slide 14

SAY: "The one that bothered me most. Updating a customer's *nickname* and
setting their *account status to terminated* are the same tool — so my engine
scored them identically. It was asking 'how dangerous is this category of
tool' when it needed to ask 'how dangerous is *this* operation'.

Now each tool carries a contract declaring per-field sensitivity. A critical,
irreversible field floors the reversibility score at 90 even though the tool's
base is 40. And an unknown field name is treated as critical — because an
undeclared field could be `account_status` under another spelling."

## 7:35 — Improvement 2  ▸ DECK slide 15

SAY: "This is the one that genuinely worried me. My red line catches one
delete of 101 records. So the agent issues 101 deletes of *one* record each —
every one individually harmless, every one autonomous. I'd converted one
dangerous operation into a hundred safe-looking ones.

Now a ledger accumulates blast radius per session and per day. **And this one
is live in production right now** — ten deletes of ten records pass, and the
hundred-and-first record trips exactly the same wire as one bulk delete."

## 7:55 — Improvement 3  ▸ DECK slide 16

SAY: "For tools my engine doesn't own, approval was just *advice* — the
caller could ask about a Slack message and delete a database instead.

Now approval issues a signed, expiring, single-use capability bound to a hash
of the exact parameters. The executor verifies it against the very bytes it's
about to run. Change one byte, it's refused. Replay it, the nonce is already
burned. Approval became enforcement."

## 8:15 — Improvements 4 and 5  ▸ DECK slides 17, 18

SAY: "Identity: one shared reviewer token meant my audit said
'authenticated-reviewer', never *who*. Now agents carry hashed API keys,
reviewers have individual tokens, and the agent id comes from the credential
instead of from the request body — which kills impersonation in three lines.

And audit: conditional writes stop a *caller* rewriting history, but not
someone with database access. So every record is now sealed with a content
hash, windows are sealed into Merkle anchors, and a verification tool names
the exact record that was altered. Tamper-*resistant* became
tamper-*evident*.

Two hundred and thirty-four tests, on the `v2-production` branch."

## 8:40 — Close  ▸ DECK slide 19

SAY: "One sentence re-derives every decision in this project: **trust is
earned, bounded, and verified — never assumed.**

The agent doesn't get trust — its counts are re-derived, its confidence
capped, its tool names normalized. The reviewer doesn't get unlimited trust —
separation of duties, edits re-governed. The learning loop doesn't — clamped
and asymmetric. Even the policy file doesn't — it's validated at boot and the
service refuses to start if it's malformed.

That's AutonomyGate. Thank you."

---

## Pre-flight checklist

- [ ] Deck open, F5 tested; Antigravity open with the six tabs above
- [ ] Dashboard hard-refreshed (`Ctrl+Shift+R`) and **signed in**
- [ ] `/health` healthy; one throwaway task run to warm the Lambda
- [ ] Approval queue EMPTY
- [ ] Alt+Tab between deck → code → browser practised twice
- [ ] Mic tested: record 10 seconds and **play it back**
- [ ] Notifications muted, unrelated windows closed

## If something goes wrong

- **Task stalls** — keep talking; Groq rate-limits and recovers in ~20s.
- **LLM refuses** — the dashboard says so explicitly now. Use it: *"that's the
  model's own guardrail, before my engine is even consulted — two different
  layers."* Then re-run with the scripted phrasing.
- **Anything else** — don't restart. One clean take with a stumble beats five
  retakes and a missed deadline.

## Running short? Cut in this order

1. Layer 2 (validation) — 35s
2. Layers 6–8 combined slide — trim to one sentence each
3. Improvements 4 and 5 — mention in one line

Never cut: the router/policy.yaml beat (3:00) or the salami-slicing
improvement (7:35). Those are the two strongest moments in the video.

## Submission package

1. Live URL: https://qg37onlhnh.execute-api.us-east-1.amazonaws.com
2. GitHub: https://github.com/Tarunswamy-Muralidharan/autonomy-engine *(public)*
3. Demo video (unlisted YouTube or Drive)
4. The reviewer token is shown on the dashboard itself, so evaluators can
   exercise the approval queue from the URL alone

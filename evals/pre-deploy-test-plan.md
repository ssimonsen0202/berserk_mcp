# Pre-deploy test plan — updated homelab MCP (spec polish)

Goal: before cutting Hermes over to the updated `berserk-mcp.py`, surface **every**
user-visible change — improvements AND regressions AND "no different but now slower/
wordier". The only behavioral variable is the `instructions` string; everything else is
inert metadata. So the tests target what `instructions` could do — including the ways it
could make things **worse**.

## Part A — Offline integrity (run BEFORE deploy, zero risk, no agent)

Proves the updated MCP didn't break the protocol or drop/rename a tool. Run on any box
with python (laptop is fine — `tools/list` doesn't touch bzrk).

Checks (all must pass or DO NOT deploy):
1. `initialize` returns `protocolVersion: 2025-06-18`; echoes a client's `2024-11-05` if sent.
2. `instructions` present and non-empty.
3. `tools/list` returns the **same 22 tool names** as the deployed version — none added/removed/renamed. (Diff the name lists.)
4. Every tool has `title` + `annotations`; `save_query.readOnlyHint == false`, `list_saved.openWorldHint == false`, all others read-only.
5. Output is valid JSON line-by-line (a client won't choke on the new keys).

Status: the additive diff already guarantees #3–#5 structurally, and #1–#2 were verified
at build time. Re-run as a gate if the file changed since.

## Part B — Behavioral A/B (the real test): deployed vs updated, same model

Run the **same questions** through Hermes on the currently-deployed MCP (capture
baseline), then deploy the update and run them again. Same cheap model (`gpt-4.1-mini`),
so the only variable is `instructions`. The deploy is reversible, so this is safe; the
rollback criteria below decide whether to keep it.

Run each question **2–3×** (tool-calling is stochastic; live metrics drift). Judge on
**tool choice + pass/fail + the cost axes**, not exact numbers.

Command per question:
```
sudo -i -u assistant hermes -z "<QUESTION>"
```
Capture for each: the answer, which tool(s) it called (visible in the phrasing/data),
wall-clock (`time` prefix), and answer length.

### Question set — balanced across three intents

**GROUP 1 — should IMPROVE with instructions (looking for a win):**
- `I1` "Give me the error breakdown across services for the last day."
  → want `errors_by_service`, NOT raw `search`.
- `I2` "What's hammering the box right now?"
  → want `host_cpu` (or a clarifying question) — NOT a silent list of containers.
- `I3` "Is anything tracking S3 fetch latency? If there's no dedicated tool, find the metric and show it."
  → want `list_metrics` / `discover_schema` then `search` (the self-extending loop).

**GROUP 2 — must stay EQUAL (regression guard — must NOT get worse):**
- `N1` "Have there been any errors in the last hour, and from which service?" → `errors_by_service`
- `N2` "Which three containers are using the most CPU?" → `top_cpu`
- `N3` "Which VM is under the heaviest CPU load right now?" → `host_cpu`
  (These are the eval-validated four; instructions must not break them.)

**GROUP 3 — could get WORSE with instructions (this is the part people skip):**
- `W1` *legit-search* "What's the p95 of `bzrk.query.execution_duration` over the last 30 minutes?"
  → there is NO fixed tool; the CORRECT move is `search`. Instructions say "prefer a
  specific tool over `search`" — **does the updated model now avoid/refuse `search` and
  flail, where the old one just ran it?** This is the #1 regression risk.
- `W2` *verbosity / latency tax* "How many hosts are reporting?"
  → trivial. Compare answer length + latency old vs new. `instructions` adds ~80 tokens
  of context to every call — quantify the cost/wordiness penalty on simple asks.
- `W3` *over-tooling* "Is the system healthy?"
  → vague. Count tool calls old vs new. Does `instructions` make it over-call
  (`discover_schema`, multiple tools) and get slower, vs a tighter old answer?

### Score sheet (fill per question, old vs new)

| Q | correct? | right tool? | raw-search misuse? | #tool calls | latency | answer length |
|---|---|---|---|---|---|---|
| I1 | | | | | | |
| … | | | | | | |

## Decision rules

**KEEP the updated MCP if ALL of:**
- N1–N3: unchanged (still correct, same tool) — no regression on the common path.
- W1: `search` still works — the model did NOT refuse/avoid it where it was the right tool.
- W2/W3: latency and answer-length increase is modest (rule of thumb: not >~30% longer/slower on the simple asks); tool-call count on vague asks didn't blow up.
- I1–I3: same-or-better (a bonus, not required to keep).

**ROLLBACK if ANY of:**
- Any N-question regresses (wrong tool, wrong/empty answer).
- W1 breaks — instructions made it duck `search` and fail a question only `search` can answer. (This would mean the "prefer specific tool" nudge is too strong; fix the wording before redeploying.)
- Unacceptable latency/verbosity blowup on W2/W3.

Rollback = restore the backup file + re-register (see `DEPLOY-PLAN.md`).

## Honest expectation (so results aren't misread)

- **Group 2 will look identical** — the cheap model is already at ceiling there. That's success, not "no point."
- **Group 1** is where any upside shows; it may be small.
- **Group 3 is the one that matters for this decision** — if `instructions` doesn't
  break `search` (W1) and doesn't materially tax simple asks (W2/W3), the deploy is a
  clean win (annotations + protocol + loop-priming for free). If W1 breaks, the fix is a
  one-line wording change to `INSTRUCTIONS` ("prefer a specific tool **when one fits**;
  use `search` for anything else"), not abandoning the change.

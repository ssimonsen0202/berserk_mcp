# Investigation decision tree: implementation spec

Status: DRAFT — brainstormed and approved 2026-08-27, not yet implemented.
Addresses issue #24 ("Investigation decision tree, composition ceiling, no
code execution"), which itself derives from `docs/berserk-dev-brief-2026-08-20.md`
§3's "composition ceiling" weakness and §5 priority item 8.

## Problem

Fixed tools can't compose across signals. An agent investigating "why is
error rate elevated" today has to manually chain `errors_by_service` →
`soc_log_spike` → `trace_find_errors` itself, deciding at each step whether
to continue and what to check next — no different from any other multi-step
task, but with none of the reproducibility guarantee every *single* fixed
tool in this project already gives. Datadog's answer to the same gap is
`execute_code` — agent-authored TypeScript in a sandbox. This project's
constraints (no agent-authored code execution, no second backend, stdlib
only) rule that out deliberately.

## The adversarial challenge (from issue #24, resolved for this scope)

*"Does a decision tree actually solve the composition problem, or just
relocate it into who authors and maintains the tree?"*

The answer is domain-dependent, and that's the actual resolution:

- **SRE/Ops fault isolation** is a good fit. The branching logic already
  exists as prose in `primers/sre.md`'s "Escalation thresholds" section
  (error rate > 10/min → investigate; > 50/min → page; CPU load > 2.0 →
  investigate). Encoding it as an executable tree isn't relocating a
  problem — it's promoting a judgment call that's already written down
  into the same fixed/deterministic/reproducible posture every other tool
  in this project already has. This is this spec's v1 scope.
- **SOC investigation** is a worse fit. Adversary behavior is less finite,
  and an analyst legitimately needs to redirect mid-investigation based on
  judgment a fixed tree can't fully anticipate. A rigid tree here is much
  more likely to hit the real version of the adversarial challenge —
  needing constant maintenance to stay current, i.e. actually relocating
  the problem. **Out of scope for v1 and probably never a *large* tree**;
  revisit only if a genuinely finite SOC playbook turns up.

## Scope (v1)

One tree only, per YAGNI: **elevated error rate**, the issue's own worked
example.

```
start
  → errors_by_service (since=<window>)
    branch: rate > 10/min?
      no  → conclude: "error rate normal, no further checks"
      yes → check_log_spike
  check_log_spike
    → soc_log_spike (since=<window>)
      branch: spike correlated with the errors_by_service window?
        no  → conclude: "error rate elevated but no correlated log spike;
                          recommend manual review"
        yes → check_traces
  check_traces
    → trace_find_errors (since=<window>)
      branch: (always concludes — this is the terminal node)
        → conclude: "error rate elevated, correlated log spike, N failing
                      traces found: <summary>" (or "no failing traces
                      found despite the log spike — investigate ingestion
                      lag or a non-trace-instrumented failure path")
```

Thresholds are sourced from `primers/sre.md`'s existing escalation table.
**Known follow-up, not blocking v1:** the primer's prose and this tree's
executable thresholds are two independent copies of the same numbers today
— nothing keeps them in sync if one changes. Worth a shared-constants
module once a second tree exists; premature for a single tree.

No second tree in v1. Candidates for v2 (not designed here): host-pressure
isolation (`sre_host_headroom` → `sre_top_error_messages`), ingestion-lag
isolation (`sre_ingest_health` → `detect_anomalies`).

## Architecture

### File organization

New module `investigation.py`, following the same pattern every other
capability lane uses (`agent_analytics.py`, `parser_factory.py`,
`ai_finops.py`): a `configure(bzrk_search, table, ...)` entry point wired
by `berserk_mcp.py` at import time, pure functions elsewhere, no import of
`berserk_mcp` itself (avoids the import cycle, keeps the module testable
standalone — same rationale `agent_analytics.py`'s own module docstring
states). The tree data structure (nodes, thresholds, branch targets) lives
in this module as a plain, hardcoded Python structure — not a config file
or DSL; a second tree is still cheap to add by hand at this size, and a
generalized tree-definition format is premature for one tree.

### Execution model

`investigate_error_rate` is a single new MCP tool (SRE lane) that
**executes each hop itself** — it does not just tell the calling agent
which fixed tool to run and wait for the agent to feed a result back in.
Internally it calls the same data-fetch code the underlying fixed tool
(`errors_by_service`, `soc_log_spike`, `trace_find_errors`) uses **before
that tool's own text-formatting step** — never the fixed tool's already-
formatted prose output. Branch evaluation always operates on the real
structured value (a number, a boolean), never on parsed display text.

This resolves a real fragility risk directly: if `investigate_error_rate`
had to parse `errors_by_service`'s human-readable output to decide whether
a branch fired, a display-format change to that tool would silently break
branch logic elsewhere. Reusing the pre-formatting data path removes that
coupling entirely.

### Step-by-step, not one-shot

Each call to `investigate_error_rate` runs **exactly one hop**, then
returns control to the caller with an explicit instruction for how to
continue (`node=check_log_spike`) or a final verdict if the tree
concluded. This is a deliberate choice, not a default:

- **Wrong-answer containment (the deciding reason).** A one-shot walk that
  takes a wrong branch at hop 1 commits silently to a bad verdict with no
  checkpoint — exactly the silent-failure risk issue #12 already treats as
  this project's one hard reliability gate. Step-by-step keeps the calling
  agent able to sanity-check each hop's result before the next branch
  commits.
- **Reuses existing infrastructure.** Matches the `next_call`-hint pattern
  already recommended in the dev brief §4(c) (copy Datadog's
  `recommended_tool_call`, populated from a static table, never
  generated) and dovetails with #14's just-in-time discovery model.

### Tool interface

```
investigate_error_rate(since="1h ago", node="start")
```

- `since`: same convention as every other tool's `since` parameter.
  Carried unchanged across all hops in one investigation (the window
  doesn't shift mid-investigation).
- `node`: which node to execute this call. Defaults to `"start"`. The
  agent passes back whatever node ID the previous response named as
  "next" to continue; a fresh call with no `node` (or `node="start"`)
  begins a new investigation.

Response is plain text, matching this project's convention everywhere
else (no JSON envelope for tool results):

```
Checked: errors_by_service (since=1h ago)
Result: 23 errors/min for checkout-service
Threshold: >10/min investigate, >50/min page
Branch: investigate (elevated)
Next: call investigate_error_rate(node="check_log_spike", since="1h ago")
      to continue, or stop here if this is enough.
```

or, at a terminal node:

```
Checked: trace_find_errors (since=1h ago)
Result: 14 failing spans found, all in checkout-service
Investigation complete.
Verdict: error rate elevated (23/min, checkout-service), correlated log
spike confirmed, 14 failing traces found — root cause is likely in
checkout-service's own request path, not a downstream dependency.
```

No server-side session state. The full "where am I" state is the `node`
string the agent already has from the previous response — matches this
project's no-daemon-required design ethos (same principle `quota_status`
states explicitly: "never requires a daemon or forwarder to be running").

### Error handling

If a hop's underlying query fails (backend unavailable, timeout, KQL
validation rejection), `investigate_error_rate` halts immediately and
reports the failure explicitly. It never silently treats a failed check as
a clean "no problem" result, and it never guesses a default branch to keep
going. Same precedent as the schema-fetcher fix (issue #32): a failed
backend call is reported as unavailable, never passed through as if it
were fresh data.

```
Checked: soc_log_spike (since=1h ago) — FAILED
Error: bzrk query timed out after 120s
Investigation halted at check_log_spike. The error-rate elevation from the
previous step (23/min, checkout-service) is still valid; this step's
result is unknown, not "no spike."
```

## Alternatives considered, not chosen

Documented per the design-review discussion; may be worth revisiting if
v1's shape doesn't hold up in practice.

- **Stateless, agent executes each hop (Option A).** `investigate(tree,
  node, input)` would tell the agent which fixed tool to call and expect
  the agent to feed the result back in for branch evaluation. Rejected:
  requires parsing the fixed tool's prose output back into structured
  values, which is exactly the fragility the chosen execution model
  avoids.
- **One generic `investigate(tree=...)` dispatcher instead of named
  per-intent tools.** Rejected in favor of one named tool per intent
  (`investigate_error_rate`), matching how every other capability in this
  project is its own named, self-describing MCP tool rather than a
  generic dispatcher — and it's naturally discoverable through `find_tool`
  (#14) without the agent needing to already know tree/intent IDs.

## Tests plan

Standard TDD, same dependency-injection seam pattern used throughout this
codebase (override the data-fetch function the tree's nodes call through,
the same way existing tests override `bm.run_bzrk`).

- **Per-node branch tests.** For each node, one test per branch outcome —
  e.g. for `start`: rate ≤ 10/min → concludes "normal"; rate > 10/min →
  advances to `check_log_spike`. Mirrors the existing per-finding test
  style in `tests/test_kql_validation.py` and `tests/test_berserk_mcp.py`.
- **Full end-to-end walk test.** One test that forces every branch to fire
  in sequence (elevated → spike → traces found) and asserts the final
  verdict text. A second walk test for the "elevated but not correlated"
  early-exit path.
- **Error-halt test.** A mocked backend failure at `check_log_spike`
  asserts the tool halts, reports the failure, and does not silently
  advance to `check_traces` or invent a "no spike" result.
- **`node` parameter validation.** An unknown/malformed `node` value
  returns a clear error rather than a stack trace or a silent restart at
  `start`.
- **Regression against the pre-formatting data path.** A test that
  deliberately changes `errors_by_service`'s *display* formatting (e.g.
  wording of the count line) and confirms `investigate_error_rate`'s
  branch logic is unaffected — proves the "operates on structured data,
  not parsed prose" architecture decision actually holds, not just that
  it's stated as a design intent.

## Evals plan

The existing eval harness (`evals/run_eval.py`, case format in
`evals/router_cases.jsonl`: `{"id", "prompt", "expect_tool"}`) measures
single-hop tool *selection* — does the model pick the right tool for a
prompt. That format directly covers the first-hop question this feature
adds:

- **New router cases, first hop only.** Add cases like `{"id":
  "investigate_error_spike", "prompt": "why is checkout-service's error
  rate elevated?", "expect_tool": "investigate_error_rate"}` to
  `evals/router_cases.jsonl`. Tests whether a model reaches for the
  investigation tool instead of manually chaining `errors_by_service` →
  `soc_log_spike` → `trace_find_errors` itself when the prompt implies a
  root-cause question rather than a single-metric one. This is measurable
  today with zero harness changes.
- **Multi-hop continuation eval — explicitly deferred, not v1.** Whether a
  model correctly reads a response's `node="check_log_spike"` instruction
  and issues the follow-up call (rather than stopping early, or
  hallucinating a different next tool) needs a multi-turn eval case format
  the harness doesn't have yet — `run_eval.py`'s current shape scores one
  prompt → one tool call, not a conversation. Building that format is real
  work belonging to issue #22/#23 (routing reliability analyzer, model-
  ladder sweep with guardrails), not this spec. Flagging honestly rather
  than silently narrowing what "evals" covers: v1 gets first-hop selection
  coverage only, not full-walk correctness under a real model. The unit
  tests above cover full-walk *mechanism* correctness (the tree's own
  logic); the eval gap is specifically "does a live model actually use it
  end-to-end," which stays open until the multi-turn harness exists.
- **No new synthetic-data risk.** Per this project's existing eval-harness
  guardrail (synthetic fixtures only, no real hostnames/customer data),
  the new router cases use the same placeholder service names
  (`checkout-service`) already used elsewhere in `router_cases.jsonl`.

## Open follow-ups (not v1)

- Shared threshold constants between `primers/sre.md`'s prose and the
  tree's executable branch conditions, once a second tree exists.
- A second SRE/Ops tree (host-pressure or ingestion-lag isolation).
- Multi-turn eval harness support (belongs to #22/#23), needed before this
  feature gets end-to-end model-reliability coverage rather than just
  first-hop selection coverage.
- Whether a SOC tree is ever worth it, and if so, how small/finite it
  would need to be to avoid the maintenance-relocation failure mode named
  above.

# Model routing and cost validation

Date: 2026-08-23

## Summary

An earlier idea note (2026-07-31, sourced from a PromptCost article on
multi-model routing) proposed routing cheap/simple work to cheap models and
escalating uncertain work to more capable ones, as an advisory/governance
role for berserk-mcp. This is the first real data against that idea: a
real-model eval sweep (local Ollama + OpenRouter, 41-case
`evals/router_cases.jsonl`, run 2026-08-22/23) measuring tool-selection
accuracy, argument accuracy, latency, and *real billed cost* (not sticker
price) across 8 models on berserk-mcp's own 69-tool schema. The table below
is the durable record of the findings -- the raw per-case result JSONs live
in `evals/results/`, which is gitignored (generated output, not committed),
so treat this document as the source of truth rather than assuming those
files persist on any given machine.

## Headline results

| Model | Tool-sel | Arg acc | Latency (median/p95) | Real cost/call (warm cache) |
|---|---|---|---|---|
| deepseek/deepseek-chat | 93% | 98% | 4.2s / 4.7s | $0.0114 (never caches) |
| stealth/ox-alpha | 93% | 93% | 7.3s / 20.8s | $0 (stealth/promotional, not durable) |
| deepseek/deepseek-v4-flash | 88% | 93% | 3.6s / 12.7s | $0.0003 (caches 5.5x) |
| mistralai/mistral-saba | 83% | 90% | 0.76s / 1.5s | $0.00056 (caches 10x) |
| mistralai/mistral-small-3.2-24b-instruct (open-weight, self-hostable) | 78% | 90% | 1.2s / 3.2s | n/a (local candidate) |
| mistralai/mistral-nemo | 63% | 85% | 1.6s / 4.0s | $0.0005 |
| *mock keyword baseline* | *65.9%* | — | — | — |
| Qwen2.5:7b (local, full tier) | 7% | 63% | — | free/local |
| Llama3.1:8b (local, full tier) | 5% | 66% | — | free/local |

## Finding 1: the reliability floor is ~24B parameters, not "small"

Local 7-8B models (Ollama) are unusable for this task even with tool-tiering
applied (tiering hides 14/69 tools, ~20% schema cut -- a real accuracy lever,
far short of closing the gap). One size class up, mistral-nemo (~12B) is
*worse than the dumb keyword-matching baseline* -- its LLM-based routing
judgment adds no value over `evals/ci_gate.py`'s trivial heuristic at that
size. The floor in this data is the 24B class: both mistral-saba (proprietary,
API-only) and mistral-small-3.2-24b-instruct (Apache 2.0, genuinely
self-hostable, confirmed on Hugging Face) clear the baseline comfortably.
"Cheap models can't do this reliably" is true only below roughly this size --
above it, several real options work.

## Finding 2: prompt caching is real, automatic, and changes the cost ranking

Two of the three top-accuracy models cache OpenRouter's OTLP-visible
`usage.cost` field at a steep discount, verified with real successive API
calls, not just an advertised rate:

- `deepseek-v4-flash`: full price $0.0017/call -> cached $0.0003/call (5.5x)
- `mistral-saba`: full price $0.0055/call -> cached $0.00056/call (10x)
- `deepseek-chat`: **does not cache at all** -- `cached_tokens=0` on three
  identical back-to-back calls, full price every time.

This matters because naive `tokens x sticker price` math is wrong by
5-10x once caching is in play -- verified directly by comparing that
calculation against the real `usage.cost` the API returns. This is also why
issue #37 was revised mid-session: the original plan (fetch pricing, compute
cost ourselves) would have reproduced this exact error; the fix instead
persists the API's own real cost per call.

**Second-order finding**: `tool_choice: "required"` (a reasonable choice --
it guarantees a tool call instead of prose) silently disables caching
entirely. Verified live: `cached_tokens=0` on every row with `required`,
restored to 99%+ with `auto`. Any cost figure that assumes caching should
also confirm which `tool_choice` a real deployment will use -- the two
matter together, not independently.

At real (cache-aware) prices, `deepseek-chat`'s 5-point accuracy edge over
`deepseek-v4-flash` costs roughly **38x more per call** once cached, not the
"trivial either way" difference a naive calculation would suggest.

## Finding 3: cost is not the binding constraint at realistic team scale

Projected for 4 SRE engineers, real cache-aware pricing, `tool_choice=auto`:

| Volume | mistral-saba | deepseek-v4-flash | deepseek-chat |
|---|---|---|---|
| Light (1,680 calls/mo) | $0.94 | $0.50 | $19.15 |
| Moderate (4,200 calls/mo) | $2.35 | $1.26 | $47.88 |
| Heavy (8,400 calls/mo) | $4.70 | $2.52 | $95.76 |

Even the most expensive real option costs under $100/month for a 4-person
team at heavy usage. The dominant cost driver per call is the fixed 69-tool
schema itself (~27K real tokens, measured directly against the live API --
not the naive 4-char/token estimate), resent on every call; this is exactly
what caching discounts.

## Recommendation

Maps directly onto the 2026-07-31 idea doc's proposed advisory role --
concrete defaults, not just the general "route cheap, escalate uncertain"
shape:

- **Default**: `deepseek/deepseek-v4-flash` -- near-top accuracy (88%/93%),
  cheapest realistic option once cached ($2.52/mo at heavy 4-engineer usage),
  reasonable latency.
- **Fast/latency-sensitive tier**: `mistralai/mistral-saba` -- sub-second
  median latency (0.76s), 83%/90% accuracy, for interactive/blocking UX
  where speed matters more than the accuracy gap.
- **Escalation tier only**: `deepseek/deepseek-chat` -- best accuracy
  (93%/98%) but ~38x the real per-call cost of the default with no caching
  offset. Reserve for cases a fast tier already flagged as ambiguous or
  failed on, matching the existing `TIER_SMALL`/`TIER_DEEP` architecture
  (issue #4) rather than using it as a blanket default.
- **Self-hosted option, if ever needed**: `mistral-small-3.2-24b-instruct`
  (Apache 2.0, ~55GB GPU RAM at bf16, less quantized) is the closest
  open-weight match to mistral-saba's accuracy tier -- confirmed genuinely
  self-hostable, unlike Saba itself which is API-only despite looking like a
  candidate at first glance.
- **Do not default to `stealth/ox-alpha`** despite matching top accuracy --
  its $0 pricing is stealth/promotional and not a durable cost basis for a
  roadmap decision; its p95 latency (20.8s) is also the worst of the
  competitive options.

## Caveats for anyone extending this data

- Sample size is 8 models, largely OpenRouter-gated to whatever the account's
  guardrail allowlist permitted at test time -- not an exhaustive market
  survey. Re-running this after any guardrail/workspace change could surface
  more candidates.
- Cost figures assume `tool_choice="auto"`; re-verify if a production
  deployment must force `tool_choice="required"` for reliability reasons --
  see Finding 2.
- `mistral-small-3.2-24b-instruct`'s numbers come from the same 69-tool
  OpenRouter eval as everything else here, not from an actual local
  deployment -- self-hosted latency/behavior is not yet verified.

## Addendum, 2026-08-29: `investigate_error_rate` re-check (70-tool schema)

`investigate_error_rate` (issue #24) shipped after the sweep above ran, so
the schema grew from 69 to 70 tools and `evals/router_cases.jsonl` grew from
41 to 42 cases (new case: `investigate_error_spike`). Only the mock
keyword router had ever tested that case before this. Re-ran the full
42-case set through the three OpenRouter models this doc recommends for
production, at `tool_choice=auto`:

| Model | Tool-sel | Arg acc | `investigate_error_spike` | Cost (42 cases) |
|---|---|---|---|---|
| `deepseek/deepseek-v4-flash` (default) | 95% (40/42) | 95% | routed correctly | $0.0240 |
| `deepseek/deepseek-chat` (escalation) | 93% (39/42) | 95% | routed correctly | $0.3286 |
| `mistralai/mistral-saba` (fast tier) | 88% (37/42) | 95% | **routed to `sre_service_health` instead** | $0.0306 |

All three scores hold steady or improve slightly versus the 08-23 sweep
(deepseek-chat 93%→93%, deepseek-v4-flash 88%→95%, mistral-saba 83%→88%) --
not a like-for-like comparison (41 vs. 42 cases), but no regression either.

**Real finding:** `mistral-saba`, this doc's recommended fast/latency tier,
does not reliably reach the new tool -- it picked a plausible neighbor
(`sre_service_health`) instead of `investigate_error_rate` on the one case
that exercises it. `deepseek-v4-flash` (the default tier) and
`deepseek-chat` (the escalation tier) both got it right. This does not
change the recommendation above, since `investigate_error_rate` sits behind
the default tier in normal operation -- but it is a concrete reason not to
route SRE-lane traffic through the fast tier alone if the caller expects
`investigate_error_rate` to be reachable. Worth re-checking after any
future tool-description change to `investigate_error_rate` or
`sre_service_health` aimed at sharpening the distinction between them.

## Addendum, 2026-08-29: `agent` parameter re-check (v1.26.0, issue #42)

`claude_recent`/`claude_sessions`/`claude_tools`/`claude_errors`/`claude_search`
took an optional `agent` parameter in v1.26.0 (issue #42), but no eval case
had ever exercised whether a real model actually supplies it. Added two
cases (`cc_agent_codex`, `cc_agent_codex_search`) that ask about "Codex
CLI" instead of "Claude Code" and expect `agent="codex"` in the call.

| Model | Both cases | Notes |
|---|---|---|
| `deepseek/deepseek-v4-flash` (default) | 2/2, exact `agent="codex"` both times | |
| `mistralai/mistral-saba` (fast tier) | 1/2 | got the plain-recent case right; on the search case it picked the raw `search` escape hatch instead of `claude_search` |

Same pattern as the `investigate_error_rate` check above: the fast tier
handles the common case but misses on a less obvious phrasing. Not a
blocker for the current recommendation, but a second data point that
`mistral-saba` is the tier to watch first if routing accuracy regresses in
production, not the default tier.

Also fixed while adding these cases: `evals/run_eval.py`'s mock keyword
router gated every `claude_*` branch on the literal substring `"claude"`,
so a prompt that says "Codex CLI" instead of "Claude Code" fell through to
the wrong default and would have silently failed the CI gate the same way
the `investigate_error_rate` case did on 2026-08-28 (see the PR #70 CI
history). Extended each branch to match `"claude" in p or "codex" in p`
before adding the new cases, verified locally with `ci_gate.py` before
either landed.

## Addendum, 2026-08-29: description fix for both `mistral-saba` misses

Both real misses recorded above (`investigate_error_rate` -> `sre_service_health`,
and the `claude_search` -> raw `search` miss) trace to specific, fixable gaps
in the tool descriptions, not a model-capability limit:

- `investigate_error_rate`'s description never said "root cause" or "why"
  anywhere, even though that's the literal language in the failing prompt.
  Added `Root-cause investigation for an elevated error rate` up front and
  `'why is X's error rate up'`/`'find the root cause'` to the use-for
  examples -- the same pattern `sre_service_health` and `sre_error_rate`
  already use for their own disambiguation.
- `claude_search` led with "Claude Code" and buried the multi-agent
  capability in a parenthetical. Named a concrete other agent (Codex CLI)
  up front instead, so the prompt's own words textually match the
  description.

Added 3 new eval cases rather than re-testing only the 2 known misses, to
avoid overfitting the fix to one example each: a second, differently-phrased
root-cause prompt (`investigate_error_root_cause_2`); a guard-rail case
confirming a plain health question still routes to `sre_service_health`,
not `investigate_error_rate` (`sre_service_health_guardrail`); and a second
cross-agent search phrasing (`cc_agent_codex_search_2`).

| Model | Before (44 cases) | After (47 cases) | Notes |
|---|---|---|---|
| `mistralai/mistral-saba` (fast tier) | 38/44 (86%) | 43/47 (91%) | both originally-targeted misses now pass; guard-rail passes (no overcorrection); zero regressions on previously-passing cases |
| `deepseek/deepseek-v4-flash` (default) | 40/42 (95%, prior addendum) | 45/47 (96%) | all 6 new/changed cases correct, no regression from the description edit |

**One residual case, deliberately not chased further:** `investigate_error_root_cause_2`
("auth-service's errors jumped this morning -- dig in and tell me what's
actually causing it") still routes to `sre_error_rate` on `mistral-saba`
even after adding a second disambiguation line for that tool specifically.
`deepseek-v4-flash` gets it right. Per the same held-out-validation
discipline this addendum is built on, this was not chased with a third
description tweak targeting this one phrasing -- that risks overfitting the
description to a single adversarial example rather than a real, general
gap. Left as a known limit; folded into issue #77's planned structured
tool-description audit rather than fixed reactively here.

## Addendum, 2026-08-29: first multi-turn eval run (issue #75)

`investigate_error_rate`'s hop-2 continuation had never been tested against
a real model -- the single-shot `router_cases.jsonl` harness structurally
can only test `node="start"`. The new multi-turn mode (`evals/run_eval.py`
+ `evals/router_cases_multiturn.jsonl`) seeds a real hop-1 fixture (built by
calling the actual server dispatch, not a hand-written string) and checks
whether the model correctly reads the fenced Result line and continues the
investigation with the right `node`/`service`.

| Model | `investigate_hop2_extract_service_checkout` | `investigate_hop2_extract_service_auth` |
|---|---|---|
| `deepseek/deepseek-v4-flash` | pass -- correctly extracted `service="checkout"` from the fenced text and continued | fail -- abandoned the explicit continuation instruction, called `sre_error_rate` instead |
| `mistralai/mistral-saba` | pass -- same correct extraction | fail -- made no tool call at all |

**What this confirms:** the round-3 Codex fix (PR #72) -- never echoing the
raw service value unfenced, instead pointing the model at the fenced Result
line -- works. Both models successfully extracted `"checkout"` out of fenced
text they were told not to treat as instructions, on the first real test of
that specific mechanism.

**What this surfaces as new:** the second case fails for both models, but
not because of multi-turn mechanics -- the prompt ("auth-service's errors
jumped -- find the root cause") is the same hard root-cause phrasing that
already failed for `mistral-saba` in single-shot mode
(`investigate_error_root_cause_2`, addendum above). Here it fails even
though the model is *handed* the correct continuation instruction in the
previous turn, which is a stronger failure than picking the wrong tool cold
-- worth noting for issue #77's audit, not a new bug in the multi-turn
harness itself.

Sample size is 2 cases against 2 models -- enough to prove the harness works
end-to-end and that the core fenced-extraction mechanism functions, not
enough to draw a general reliability number. More cases belong to whoever
picks up issue #77 or extends this file.

## Addendum, 2026-08-29: multi-server setting (issue #78)

Every eval this project has run before this tests berserk-mcp's tools in
isolation. Real agents usually don't work that way -- an agent asking about
`checkout-service`'s error rate plausibly also has a Slack or GitHub MCP
server loaded in the same context. Multiple 2026 MCP benchmarks (MCP-Bench,
MCP-Atlas) explicitly test this and report that "strong models maintain
stable performance across multi-server settings, while weaker/small models
show clear degradation."

Added `--with-foreign-tools` to `evals/run_eval.py`: appends a fixture
9-tool Slack/GitHub-shaped schema (`evals/foreign_tools_fixture.py`, never
executed, just present as schema noise) alongside berserk-mcp's real tools.
Ran the full 47-case set against the three production-recommended models,
isolated vs. combined:

| Model | Isolated | Combined | Delta |
|---|---|---|---|
| `deepseek/deepseek-v4-flash` (default) | 45/47 (96%) | 44/47 (94%) | -2pp |
| `deepseek/deepseek-chat` (escalation) | 43/47 (91%) | 46/47 (98%) | **+6pp** |
| `mistralai/mistral-saba` (fast tier) | 43/47 (91%) | 41/47 (87%) | -4pp |

**This is a mixed result, reported honestly rather than fit to the
hypothesis.** `mistral-saba` -- already the weakest of the three -- shows
the clearest degradation, consistent with the literature. `deepseek-v4-flash`
shows a small drop. `deepseek-chat` improved, which contradicts the simple
"weaker models degrade more" story. Each run here is a single pass
(`--repeats 1`, matching every other eval in this document); a few
percentage points on a 47-case set is 1-3 individual cases, and real API
sampling isn't perfectly deterministic even at `temperature=0`. This is not
enough data to confidently separate a true multi-server effect from
run-to-run noise -- averaging over `--repeats 3` or more, for whoever
extends this, would answer that properly.

**One concrete finding worth flagging regardless of the aggregate number:**
on `mistral-saba`, `investigate_error_spike` -- the flagship case for the
tool this whole session's earlier work focused on -- passes in isolation
but fails with foreign tools present, routing to `sre_error_rate` instead.
The same case is fine on both other models in both conditions. This is a
real, reproducible instance of the effect the benchmarks describe, even if
the aggregate numbers above don't universally confirm it: a tool that
correctly routes in isolation can still lose to an unrelated tool's noise
once the schema gets bigger, on the weaker tier specifically.

### Reproducing this

```bash
python3 evals/run_eval.py evals/router_cases.jsonl --backend openai \
  --base-url https://openrouter.ai/api/v1 --key-env OPENROUTER_API_KEY \
  --model <model> --tool-choice auto --with-foreign-tools
```

## Addendum, 2026-09-01: model-behavior monitoring's noise-band calibration

The model-behavior monitoring feature (issues #89-#92) needed a real
measurement of the canary's own run-to-run variance before
`model_drift.DEFAULT_NOISE_BAND` could be set to anything but a guess. That
measurement also surfaced a real bug in the feature itself, worth recording
alongside the data.

**The bug, found before any live run could succeed:** `canary.run_canary()`
defaulted to `backend="openai"` with no `base_url`/`key_env`, which
`run_eval.py` itself resolves to `https://api.openai.com/v1` +
`OPENAI_API_KEY` when neither is given. That endpoint has no
`vendor/model`-style IDs and needs a different key entirely, so every
canary run -- including the `--canary-run` CLI path -- would have failed
immediately once actually exercised. This was in the original implementation
plan's own text, so it passed every task review and the final whole-branch
review; nothing in that process could run it live to notice. Fixed by
defaulting `base_url`/`key_env` to this project's own already-configured
Hermes provider (`parser_factory._hermes_url()` / `HERMES_API_KEY`) when
neither is given explicitly, and by defaulting `tool_choice` to `"auto"`
instead of `run_eval.py`'s own `"required"` default -- `"required"` is the
same DeepSeek prompt-caching killer documented in Finding 2 above.

**The calibration itself:** 5 consecutive live canary runs against
`deepseek/deepseek-v4-flash` (`repeats=3`, the full 48-case
`evals/canary_cases.jsonl`, `case_set_version 61a845d75929`), run
sequentially -- `_run_harness()` identifies its own output by "newest file
in `evals/results/`", so concurrent runs would race and grab each other's
results.

| Run | tool_accuracy | arg_accuracy | cost (USD) | wall time |
|---|---|---|---|---|
| 1 | 0.9514 | 0.9444 | $0.0837 | 460.6s |
| 2 | 0.9583 | 0.9444 | $0.0774 | 482.6s |
| 3 | 0.9514 | 0.9514 | $0.0764 | 509.5s |
| 4 | 0.9514 | 0.9444 | $0.0824 | 479.9s |
| 5 | 0.9444 | 0.9375 | $0.0788 | 440.5s |

All 5 runs succeeded (`eval.status: "ok"`) at the same `case_set_version`.
tool_accuracy: mean 0.9514, stdev 0.0049, range 0.0139 (min 0.9444, max
0.9583) -- max single-run deviation from the mean was 0.0069, symmetric on
both sides. Total cost across the 5 runs: $0.3987. Total wall time: ~40
minutes (a single run over the full case set at `repeats=3` takes roughly
7.5-8.5 minutes sequentially against a live provider -- plan around that,
this is not a quick check).

`DEFAULT_NOISE_BAND` is set to `0.02` -- roughly 3x the maximum observed
single-run deviation, rounded to a clean number. It comfortably clears the
measured noise (confirmed: the 5 calibration runs classify as `stable`
against each other under this band) while remaining tight enough to catch
a regression that would actually matter. This is a starting point from one
model's one calibration run, not a permanent constant -- re-baseline if the
case set changes size materially, or once enough real production history
accumulates to compare against.

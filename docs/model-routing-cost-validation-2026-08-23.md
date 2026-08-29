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

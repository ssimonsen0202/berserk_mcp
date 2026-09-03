# Optimization plan: `mistral-small-3.2-24b-instruct` as the sovereign tier

Date: 2026-09-03

## What this optimizes for

`mistralai/mistral-small-3.2-24b-instruct` is the only model this project has
tested that is both **open-weight (Apache 2.0) and genuinely self-hostable**,
and that clears the keyword-match baseline. It is therefore the model a
sovereign or air-gapped deployment actually depends on. Its recorded score is
**78% tool-selection / 90% argument accuracy**
([docs/model-routing-cost-validation-2026-08-23.md](model-routing-cost-validation-2026-08-23.md)).

That 78% is the number this plan exists to improve. But two corrections come
first, because both suggest the real gap is smaller than 78% implies -- and
optimizing against a wrong number wastes the whole effort.

## Correction 1: the 78% is stale

It was measured on 2026-08-23, against a 69-tool schema and a 41-case set,
before every tool-description fix this project has since shipped.

Its closest sibling, `mistral-saba` (same vendor, proprietary), was measured
on that same sweep at 83%. Tracked across the same fixes since:

| Date | Case set | `mistral-saba` |
|---|---|---|
| 2026-08-23 (original sweep) | 41 | 83% |
| 2026-08-29 (`investigate_error_rate` re-check) | 42 | 88% |
| 2026-08-29 (after description fixes) | 47 | 91% |
| 2026-09-03 (pre-fix, current set) | 51 | 86.3% |
| 2026-09-03 (after reciprocal disambiguation) | 51 | **92.2%** |

`mistral-saba` gained roughly 9 points from description work alone, with no
model change. `mistral-small` has never been re-measured against any of it.
**If it tracks its sibling at all, a meaningful part of the 12-point gap to
90% may already be closed.** Nothing in this repo knows, because nobody has
re-run it.

## Correction 2: 78% was measured in the worst possible configuration

That sweep sent the **full** tool schema. A real sovereign deployment runs a
role lane. Measured today against the current codebase:

| Configuration | Tools | Est. schema tokens |
|---|---|---|
| `all` (default) | 74 | ~29,000 |
| `all` + `tier=small` | 61 | ~24,000 |
| `claude` | 46 | ~18,100 |
| `sre` | 32 | ~12,100 |
| `soc` | 31 | ~11,400 |
| `ops` | 23 | ~8,100 |

Token figures are estimates, scaled from this project's own measured anchor
(69 tools ≈ 27K real tokens, measured against the live API -- Finding 3 of the
routing-validation doc). They are not independently measured per lane yet.

This matters because of published threshold data: tool-selection accuracy
degrades sharply with tool count, with **~30 tools identified as the point
where descriptions begin to overlap and cause confusion**, and ~19 described
as a sweet spot ([tool scalability patterns](https://github.com/joshrotenberg/tower-mcp/issues/514),
[Speakeasy: why less is more for MCP](https://www.speakeasy.com/mcp/tool-design/less-is-more/)).
At 74 tools the sweep measured this model in a regime the literature expects
to fail. At `ops` (23) or `sre` (32) it sits at or near the threshold instead.

**Also found while measuring this:** `tier=small` reduces the schema only for
`role=all` (74 → 61). For every role-scoped configuration it changes nothing.
Either that is intended and undocumented, or tier gating has a gap. Worth a
separate look; not a blocker here.

## The hard constraint nobody has reconciled yet

The dev brief names the local production target explicitly: **Q4_K_M at 8k
context** ([docs/berserk-dev-brief-2026-08-20.md](berserk-dev-brief-2026-08-20.md)).

Against the table above, **no configuration fits 8k context**. The smallest
role lane (`ops`, ~8.1K estimated tokens) consumes the entire window with the
tool schema alone, before any system primer, user prompt, or reply. `sre` is
~1.5x over. The full schema is ~3.6x over.

This is a genuine blocker for the stated sovereign target, and it is
independent of accuracy. Three ways out:

1. **Run local at a larger context.** Most 24B-class models support well past
   8k; 8k is a VRAM choice, not a hard model limit. Costs VRAM, changes the
   hardware budget the README currently publishes.
2. **Discovery mode** (`BERSERK_MCP_DISCOVERY=1`): 8 anchor tools plus
   `find_tool`, measured at ~1,386 tokens versus ~17,560 for the full schema
   in v1.26.0. This is the only option in the table that comfortably fits 8k.
3. **Split into domain-scoped servers** -- the "3-5 focused servers rather
   than one monolith" pattern
   ([Speakeasy](https://www.speakeasy.com/mcp/tool-design/less-is-more/)).
   Architecturally the largest change; not scoped here.

**Option 2 creates a tension this plan must own.** Testing `find_tool`'s
retrieval directly on 2026-09-03 (see the routing-validation doc's addendum)
showed its TF-IDF keyword matcher shares the exact lexical-collision failure
mode as tool descriptions: on one real case (`jworkflow_hotspots_2`) it
dropped the correct tool out of the top-5 candidate window entirely. So if the
sovereign path needs discovery mode to fit its context budget, then improving
`find_tool`'s retrieval -- from keyword/TF-IDF toward embedding-based
similarity, the pattern MCP's own guidance recommends
([Semantic Tool Discovery for MCP](https://arxiv.org/pdf/2603.20313)) --
stops being optional polish and becomes load-bearing.

## Plan

### Phase 0 -- Re-baseline before optimizing anything

Cost: ~$0.05 in API spend. Effort: hours.

1. Run `mistral-small-3.2-24b-instruct` against the current 51-case set, full
   schema, current descriptions, via OpenRouter. Compare directly to 78%.
2. Run it per role lane, scoring only the cases whose expected tool is visible
   in that lane. Case counts available today: `sre` 27, `claude` 38, `soc` 21,
   `ops` 17.

**Exit criterion:** a current, real number for the configuration a sovereign
deployment would actually run. Every later phase is planned against that, not
against 78%.

**Risk:** OpenRouter's shared free-tier pool rate-limits some models
(`deepseek-chat` was unrunnable on 2026-09-03 for this reason). Whether
`mistral-small` is affected is unknown. Retry-with-backoff and
`--call-delay-ms` are already in the harness; if the pool blocks it anyway,
Phase 0 needs a paid key or a local run, which reorders this plan around
Phase 4.

**Harness note:** role scoping needs no code change. `run_eval.py` spawns
`berserk_mcp.py` without an `env=` override, so it inherits `BERSERK_MCP_ROLE`
from the calling shell. Per-lane case filtering does need a small addition --
scoring a `claude_*` case under `role=sre` is an automatic, meaningless
failure, since the tool is not offered at all.

### Phase 1 -- Offline collision analysis

Cost: $0, no API calls. Effort: hours.

Systematize what was done ad hoc on 2026-09-03. Use `tool_discovery`'s own
TF-IDF index to compute pairwise lexical proximity across all 74 tools and
rank the closest pairs. This finds the next
`claude_workflow_insights`/`claude_token_burn`-shaped collision **before** a
model hits it.

This inverts the project's current posture. Every description fix so far has
been reactive -- fixed only after a real eval miss exposed it. This is the
proactive counterpart, and it is free to run.

**Honest caveat:** lexical proximity is a candidate generator, not a verdict.
A flagged pair still needs a real eval case to confirm it is a genuine routing
risk. Do not fix a pair on proximity alone; that reintroduces exactly the
speculative description churn
[docs/tool-description-audit.md](tool-description-audit.md) warns against.

**Deliverable:** a script under `evals/`, plus a ranked at-risk pair list
feeding Phase 3.

### Phase 2 -- Decide and document the deployment shape

Cost: $0 beyond Phase 0. Effort: hours.

Combine Phase 0's per-lane numbers with the threshold research and the 8k
context constraint, and answer plainly: **which configuration should a
sovereign deployment actually run?** Candidates are `ops` (23 tools, nearest
the sweet spot), `sre` (32, at the threshold), or discovery mode (fits 8k,
but with the retrieval weakness above).

The README's sovereign section currently says to budget for a ≥24B model and a
GPU. It says nothing about which role lane to run, and nothing about the
context-budget problem. Both belong there.

**Exit criterion:** a documented, numbers-backed recommendation in the README,
replacing an accuracy claim that is currently silent on configuration.

### Phase 3 -- Targeted description work

Cost: ~$0.10 per iteration. Effort: days, incremental.

Only for collisions Phase 0 or Phase 1 actually surfaces. Use the pattern
proven on 2026-09-03, in this exact order -- each step earned its place from a
measured failure:

1. **Reciprocal disambiguation on both sides of the collision.** One-sided
   broadening of the target tool measured *zero* effect on `deepseek-v4-flash`
   and net-zero on `mistral-saba`. Adding "not X -- see Y for that" to the
   *competing* tools is what worked.
2. **Held-out variant cases**, not just the failing prompt, so the fix is
   tested for generalization rather than overfitted to one phrasing.
3. **A guardrail case for the competing tool**, confirming its own territory
   is not hijacked by the broadened neighbour.
4. **Before/after against at least two models, with a zero-regression
   requirement.**

Prioritize the ~39 tools the 6-axis rubric audit has never covered (SOC, core,
discovery, learning-loop, parser-factory, CanonLoom lanes), but order the work
by Phase 1's collision ranking rather than lane by lane.

Published industry data puts well-structured descriptions with usage examples
at a **40-60% reduction in incorrect tool calls**
([Speakeasy](https://www.speakeasy.com/mcp/tool-design/less-is-more/)). The
measured lift on 2026-09-03 (86.3% → 92.2% and 90.2% → 94.1% on the affected
cases) is consistent with that range. This is the highest-return lever
available, not a stopgap.

### Phase 4 -- Local parity, which the dev brief already mandates

Cost: hardware time. Effort: unknown until hardware is confirmed.

The dev brief is explicit: *"Re-run the top rungs locally and publish the
delta. Nothing goes out externally without it."* That step has never been
done. Every number this project publishes for `mistral-small` comes from
OpenRouter hosting, at that provider's quantization -- not from a local
deployment at Q4_K_M.

Requires: the same 51-case set through the same harness (`--backend ollama` or
`lmstudio`, both already supported), on real hardware, at the intended
quantization, then publishing the OpenRouter-versus-local delta.

**Open question that blocks scheduling this:** is there hardware for it?
~55GB GPU RAM at bf16, or materially less at Q4_K_M. Unknown from this repo.
Until answered, Phase 4 has no date, and every self-hosting claim in the
README stays formally unverified -- which the README already says, and should
keep saying until this runs.

## What this plan deliberately does not do

- **No fine-tuning.** Disambiguation-focused fine-tuning is real and published
  ([arXiv 2507.03336](https://arxiv.org/html/2507.03336)), but it breaks this
  project's core premise that any capable model works without training.
- **No clarification-question loop.** Uncertainty-driven clarification
  ([SAGE-Agent / ClarifyBench](https://arxiv.org/html/2511.08798v2)) conflicts
  directly with the fixed-query design goal: small models should route
  reliably in one shot, not negotiate.
- **No chasing the known residual cases.** `investigate_error_root_cause_2`
  and the remaining `jworkflow_*` misses stay open. Both the audit doc and the
  routing-validation doc already ruled that further description tweaks aimed at
  single adversarial phrasings risk overfitting. That ruling stands.

## Sequencing

Phase 0 gates everything. It is cheap, fast, and may show the gap is far
smaller than 78% suggests -- or that role scoping alone closes it. Phase 1 runs
free and in parallel. Phase 2 needs both. Phase 3 is incremental and
open-ended. Phase 4 is blocked on a hardware answer this repo does not have.

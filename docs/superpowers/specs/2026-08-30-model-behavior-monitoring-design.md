# Model-behavior monitoring: drift, provider updates, quality regressions

**Date:** 2026-08-30
**Status:** approved design, not yet implemented

## Problem

berserk-mcp measures its own cost and workflow behavior in depth through the
`claude_*` lane. It does not measure whether the *models it depends on* still
behave the way they did when they were chosen.

`claude_model_fit` is sometimes mistaken for this. It is not. It compares a
model's coarse tier (`frontier`, `mid`, `cheap`) against a complexity proxy
for one session. It answers "is this model the right size for this work". It
cannot answer "does this model still work as well as it did last month".

The failure this design targets is the one infrastructure monitoring cannot
see: a model returns the same status code, at the same latency, for the same
cost, and gives worse answers. `evals/ci_gate.py` does not catch it either,
by design — it runs the `mock` backend against an absolute 65% threshold, so
it catches *our* router regressing, never the *provider's* model degrading.
Its own docstring calls it "the cheap half".

The external trigger for this work is the BROCS "Observe" capability
(<https://brocs.fyi/observe/>), whose model-behaviour sub-capability names
three distinct things: drift, unannounced provider updates, and quality
regressions. Its operating test — "test whether internal measurements detect
a 20 percent quality drop before a customer reports it" — is the one test
berserk-mcp fails outright today.

## Goals

1. Detect a measurable drop in tool-routing quality for a production model,
   within one day, without a human running anything.
2. Detect that a provider changed the model behind a stable model name.
3. Distinguish a sudden step change from gradual drift, and report which.
4. Never fire on ordinary run-to-run noise.

## Non-goals

- **Blocking or auto-remediation.** This system alerts and records. It does
  not fail builds, and it does not re-route traffic. Escalation-policy
  changes remain a human decision, consistent with how
  `claude_harness_recommendations` already surfaces evidence rather than
  acting on it.
- **General model-quality measurement.** See Limitations.
- **Replacing `evals/ci_gate.py`.** That gate keeps its job (our code, mock
  backend, absolute threshold). This is the other half (provider's model,
  real backend, relative to its own history).

## Three mechanisms

The three things BROCS bundles together need three different detectors.
Conflating them is the common failure mode: a single "quality score" cannot
tell you *why* it moved.

### Mechanism 1 — scored canary (quality regressions)

A daily run of the existing harness (`evals/run_eval.py`) against the real
backend for each production model, scoring `tool_accuracy` and
`arg_accuracy`.

**The frozen case set is load-bearing.** The canary must not read
`evals/router_cases.jsonl`. That file is expected to grow (issue #13's
Phase 2 adds targeted phrasings). If the canary used it, a score drop would
conflate "we added harder cases" with "the model got worse", and the whole
signal becomes uninterpretable.

Therefore: a new **`evals/canary_cases.jsonl`**, seeded as a copy of
`router_cases.jsonl` as it stands at implementation time, and thereafter
frozen. It changes only on a deliberate baseline reset, which bumps a
`case_set_version` string stored with every result. Results carrying
different `case_set_version` values are never compared; the verdict logic
reports `insufficient-data` for a model whose history does not yet contain
enough runs at the current version.

### Mechanism 2 — provider fingerprints (unannounced provider updates)

Two independent signals, because either alone has a blind spot.

**2a. Provider metadata fingerprint.** Poll the provider's `/models`
endpoint and snapshot the entry for each canaried model: id, context length,
pricing fields, and any version or created-at metadata the provider exposes.
Hash the normalized snapshot; diff daily. A changed context length or price
is a strong signal the provider changed something.

Reuse `parser_factory.hermes_models_url()` to derive the endpoint. That
helper is suffix-based and depth-agnostic (fixed 2026-08-29 in PR #88
precisely because a positional derivation broke on real providers), so it
already works for any OpenAI-compatible provider, not just the localhost
default.

**2b. Behavioral fingerprint.** Send a small set of fixed prompts at
temperature 0 and hash the completions. Near-free. Catches a silent weights
swap in cases where `/models` metadata did not change.

Reported honestly as **"behavioral fingerprint changed"**, never as "the
provider swapped the model". Temperature 0 is not guaranteed deterministic
across providers — batching and hardware nondeterminism can change output
without any model change. This is a signal to investigate, not proof, and
the tool output and docs must both say so.

### Mechanism 3 — trend analysis (drift)

Over the stored score series, using Berserk's native `series_fit_line`
(the same R² and slope pattern `claude_cost_report` already uses for its
burn-trend verdict), distinguish two shapes:

- **Step change** — a sharp drop between consecutive runs.
- **Gradual drift** — a negative slope with adequate R² across the window.

**Correlation is what makes this trustworthy.** A step change that
co-occurs with a fingerprint change from Mechanism 2 is high-confidence
"the provider changed the model". Either signal alone is weaker and must be
reported as such.

## Noise handling

This is the largest correctness risk in the design, and it is the reason
most canaries end up ignored.

`evals/router_cases.jsonl` currently holds 48 cases. At `repeats=1`, one
case flipping moves accuracy by ~2.1 percentage points. Alerting on that
produces constant false alarms and the alert stops being read.

**Thresholds must be measured, not guessed.** Implementation begins by
establishing the natural run-to-run spread: run the canary repeatedly
against a known-stable model over several days and record the observed
variance. Alert thresholds are then set from that measured distribution.
Shipping a guessed threshold is the failure mode to avoid — the same
mistake the superseded "7B is the sweet spot" guidance made before the real
eval sweep corrected it.

The verdict logic requires all of:

1. `repeats > 1` per run, to reduce single-sample variance.
2. A drop exceeding the measured noise band by a stated margin.
3. Degradation sustained across at least two consecutive runs.
4. Enough history at the current `case_set_version` — otherwise the verdict
   is the explicit value `insufficient-data`, never a guess.

## Data model and storage

No new storage. Canary results are emitted as OTLP logs into the same
Berserk instance everything else already uses, under a distinct
`service.name` so they never mix with production telemetry:

- `service.name = "berserk-mcp-eval"`

Per-record attributes:

| Attribute | Meaning |
|---|---|
| `eval.model` | model identifier as sent to the provider |
| `eval.backend` | `openai` (OpenAI-compatible/OpenRouter) or `anthropic` |
| `eval.case_set_version` | frozen case-set version this score belongs to |
| `eval.tool_accuracy` | fraction, 0–1 |
| `eval.arg_accuracy` | fraction, 0–1 |
| `eval.repeats` | repeats per case in this run |
| `eval.total_cost_usd` | measured run cost |
| `eval.behavioral_fingerprint` | hash of temperature-0 completions |
| `eval.provider_metadata_fingerprint` | hash of the `/models` snapshot |
| `eval.run_id` | unique id for the run |

`run_eval.py` already emits `tool_accuracy`, `arg_accuracy`, `model`,
`backend`, `repeats`, `total_cost_usd`, `total_input_tokens`, and
`total_output_tokens` in its results JSON, so the scored half of this
mapping is a translation, not new measurement.

### One required change to an existing function

`ai_finops.emit_otlp_records(records, service_name)` is the correct reuse
point — it already accepts an arbitrary `service_name`, validates the
endpoint through the shared HTTP policy layer, and posts a well-formed OTLP
payload. Two of its current behaviors do not fit this caller:

- `timeUnixNano` is hardcoded to *now* for every record. The canary needs
  the actual run timestamp, and a re-send or backfill would otherwise
  stamp historical runs as current.
- The scope name is hardcoded to `berserk-mcp.ai-finops`, which would
  mislabel eval records.

Both get optional parameters that default to today's behavior, so every
existing `ai_finops` call site is unaffected. Existing callers must be
checked rather than assumed unaffected.

## Exposure: two tools, deliberately

Tool count is not free. This project's own eval data (2026-08-22/23)
measured tool-selection accuracy collapsing as the schema grows: 7-8B models
scored 5-7% against the full schema, below a 66% keyword-matching baseline.
The server currently registers 72 tools. Adding five model-monitoring tools
would erode the exact property berserk-mcp exists to protect.

Two tools, both on the `claude` role lane (which already covers AI
operations and FinOps; the `ops` lane sees everything):

| Tool | Answers |
|---|---|
| `model_drift_check` | Current status per canaried model: latest scores, trend verdict, fingerprint status, and one of `stable` / `degrading` / `step-change` / `insufficient-data`. |
| `model_drift_history` | The score and fingerprint series for one model over time, for investigating a flagged verdict. |

Both follow the existing convention of returning a human-readable summary
plus a structured JSON envelope carrying schema version, exactness, and
freshness metadata.

The `find_tool` just-in-time discovery mode (`BERSERK_MCP_DISCOVERY=1`)
already mitigates schema growth for small models; these two tools must be
reachable through it, which the existing recall-gate test
(`tests/test_tool_discovery.py`) enforces automatically for every shipped
tool.

## Alerting and headless operation

Reuses the established pattern exactly — headless CLI flag, cron, Discord
bridge, non-zero exit for alerting transports:

- `berserk-mcp --canary-run` — execute the canary for configured models,
  ingest results into Berserk. Intended for a daily cron entry.
- `berserk-mcp --drift-report` — evaluate stored history, print a summary,
  exit non-zero when any model's verdict is `degrading` or `step-change`.

Discord alerting goes through the existing bridge and stays entirely off
unless `BERSERK_DISCORD_ALERT_SECRET` is set, matching current behavior. As
with the discovery worker, a run with nothing noteworthy posts nothing, so a
stable week generates no traffic.

Configuration follows the existing environment-variable convention:

| Variable | Default | Purpose |
|---|---|---|
| `BERSERK_MCP_CANARY_MODELS` | unset | Comma-separated models to canary. Unset means the canary does nothing, so this feature is entirely opt-in. |
| `BERSERK_MCP_CANARY_REPEATS` | `3` | Repeats per case, feeding the noise handling above. |
| `BERSERK_MCP_CANARY_CASES` | `evals/canary_cases.jsonl` | Frozen case-set path. |

## Limitations, to be documented rather than discovered

1. **This measures tool-routing quality specifically, not general model
   quality.** A model could degrade at prose, reasoning, or code while
   holding tool-selection accuracy steady. The canary would not see it. The
   tool descriptions and docs must state this boundary plainly.
2. **The behavioral fingerprint is a signal, not proof** (see Mechanism 2b).
3. **Canary runs cost real money.** Measured at ~$0.002 per run for
   `deepseek-v4-flash` on 2026-08-29. Daily across 2-3 models is a few cents
   per month, but it is not zero and it is a live external dependency.
4. **A provider outage looks different from degradation.** Failed runs must
   be recorded as failures, never as a score of zero, or an outage would
   masquerade as a catastrophic quality regression.

## Verification

- Unit tests for the pure decision logic — verdict classification, the
  noise-band rule, `case_set_version` mismatch handling, and step-change vs
  gradual-drift separation — with no live provider or daemon, matching the
  existing `evals/test_ci_gate.py` pattern.
- Unit tests for fingerprint normalization and hashing, including that an
  irrelevant field reordering does not change the hash.
- A live smoke run against one real model, confirming records land in
  Berserk under `service.name="berserk-mcp-eval"` and that
  `model_drift_check` returns `insufficient-data` before enough history
  exists and a real verdict after.
- Regression check that existing `ai_finops` OTLP emission is unchanged by
  the added optional parameters.

## Implementation decomposition

Four issues, dependency-ordered, matching this project's issue-per-feature
convention:

1. **Frozen canary case set, daily scored run, Berserk ingestion.**
   Includes the `emit_otlp_records` parameter addition and the
   variance-baselining measurement that later thresholds depend on.
   Everything else depends on this.
2. **Provider-update detection** — both fingerprints. Independent of the
   scoring work once ingestion exists.
3. **Drift and regression verdict logic**, including the noise handling.
   Needs 1 for data and 2 for correlation.
4. **Two MCP tools, Discord alerting, headless CLI flags.** Needs 3.

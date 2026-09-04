# Brief: is a berserk-specific trained model viable?

**Prepared 2026-09-03 for a model with no internet access.** Everything needed
to reason about this problem is embedded below. Where a number is measured, it
says so and gives the source; where it is an estimate or an unknown, it says
that too. Please preserve that distinction in your output — this project's
convention is that unverified claims are labelled, not smoothed over.

---

## 1. What you are being asked to produce

A plan for **selecting and training a small, open-weight model specialised to
drive berserk-mcp**, as a viable alternative to the general-purpose models
currently used via OpenRouter.

Specifically, address:

1. **Is it viable at all?** Given the measured data below, is a specialised
   small model likely to beat the current best option, and at what size?
2. **One model, or one per role lane?** Is a single model covering all lanes
   the right shape, or does the measured per-lane structure argue for
   per-lane specialists?
3. **What training data, and where from?** In particular, whether and how the
   telemetry already in the Berserk cluster can be used (Section 6).
4. **What is the smallest experiment that would falsify the idea cheaply?**
   Prefer a plan that fails fast and early over one that only pays off after
   a large investment.

Assume the reader is technically strong but has not seen this codebase.
Include concrete commands, file paths, and decision criteria — this project's
convention for handoff documents is implementation-grade (signatures, exact
values, acceptance criteria), not scoped issues.

---

## 2. What berserk-mcp is

An MCP (Model Context Protocol) server, ~74 tools, pure Python standard
library, that lets an LLM answer observability questions against **Berserk**
— a self-hosted, OTEL-native, schemaless observability engine queried with a
Kusto-style language (KQL).

**The load-bearing design decision:** the model never writes KQL. Each tool
wraps one fixed, pre-verified KQL query. The model's only job is to **pick the
right tool and a time window**. This is what makes small models usable at all
— the task is closer to intent classification than to code generation.

So the model's job, precisely:

```
input:  a natural-language question + a schema of N tool definitions
        (name, description, JSON inputSchema)
output: one tool call — the tool name, plus arguments
```

**Scoring** (this is what all numbers below mean):
- **tool-selection accuracy** — did it pick the right tool? This is the
  primary metric.
- **argument accuracy** — were the arguments right (e.g. `service="checkout"`,
  `since="1h ago"`) where the case specifies expected args?

### Role lanes and tiers (important for the per-lane question)

Tools are tagged with **role lanes**. `BERSERK_MCP_ROLE` filters which tools
are visible at all:

| Lane | Tools visible | Purpose |
|---|---|---|
| `all` (default) | 74 | everything |
| `claude` | 46 | Claude Code / agent session analytics, cost, token burn |
| `sre` | 32 | service health, error rates, hosts, CPU/memory, traces |
| `soc` | 31 | security ops — log spikes, severity, new services, timelines |
| `ops` | 23 | day-to-day operational queries |

There is also a **tier** mechanism (`BERSERK_MCP_TIER=small|deep`) that hides
expensive/rare tools, and a **discovery mode**
(`BERSERK_MCP_DISCOVERY=1`) that exposes only 8 anchor tools plus a
`find_tool(intent)` search entry point instead of the full catalog.

Note: tier resolves to `small` automatically for any non-`all` role, so
setting it explicitly is a no-op in role-scoped configs.

---

## 3. The measured data (this is the evidence base)

### 3.1 The 2026-08-23 sweep — 8 models, 41 cases, 69-tool schema

All via OpenRouter, `tool_choice=auto`, real billed cost:

| Model | Tool-sel | Arg acc | Latency (median/p95) | Real cost/call |
|---|---|---|---|---|
| `deepseek/deepseek-chat` | 93% | 98% | 4.2s / 4.7s | $0.0114 (never caches) |
| `stealth/ox-alpha` | 93% | 93% | 7.3s / 20.8s | $0 (promotional) |
| `deepseek/deepseek-v4-flash` | 88% | 93% | 3.6s / 12.7s | $0.0003 (caches 5.5x) |
| `mistralai/mistral-saba` | 83% | 90% | 0.76s / 1.5s | $0.00056 (caches 10x) |
| **`mistralai/mistral-small-3.2-24b-instruct`** (open-weight, self-hostable) | **78%** | 90% | 1.2s / 3.2s | n/a — local candidate |
| `mistralai/mistral-nemo` (~12B) | 63% | 85% | 1.6s / 4.0s | $0.0005 |
| *mock keyword-match baseline* | *65.9%* | — | — | — |
| `Qwen2.5:7b` (local, Ollama) | **7%** | 63% | — | free/local |
| `Llama3.1:8b` (local, Ollama) | **5%** | 66% | — | free/local |

**Read these two facts carefully, they shape everything:**

- **7-8B models score 5-7%** — catastrophically below a trivial keyword
  matcher (65.9%). Not "worse", *unusable*. Public tool-calling benchmarks
  (BFCL) rank these families well; those benchmarks use far smaller tool
  counts and do not predict behaviour at this schema size.
- **~12B (`mistral-nemo`) scores 63% — still below the keyword baseline.**
  So the failure is not linear in size; there is a cliff somewhere between
  12B and 24B for this task at this schema size.

`mistral-small-3.2-24b-instruct` (Apache 2.0, ~55GB GPU RAM at bf16) is the
**only open-weight, genuinely self-hostable model tested that clears the
baseline**. `mistral-saba` scores higher but is proprietary and API-only.

### 3.2 The per-lane measurement, 2026-09-03 — this is the most important table

`mistral-small-3.2-24b-instruct`, same cases, varying only which lane is
active. "Full schema" and "lane schema" columns use **the same cases**, so
this isolates schema size from case difficulty:

| Lane | Tools | Cases | Full schema (74 tools) | Lane schema | Delta |
|---|---|---|---|---|---|
| `ops` | 23 | 17 | 94% | **100%** | +5.9 |
| `sre` | 32 | 27 | 96% | **96%** | 0 |
| `soc` | 31 | 21 | 95% | **95%** | 0 |
| `claude` | 46 | 38 | 74% | **89%** | **+15.8** |
| `all` | 74 | 51 | **80.4%** | — | — |

**The mechanism is not "fewer tools is better."** `sre` and `soc` scored
96%/95% *at the full 74-tool schema* — scoping gained them nothing. The whole
penalty sits in the `claude` lane. Inspecting which cases flipped explains
why: 4 of 6 were lost to a tool **from a different lane** sharing vocabulary
(e.g. `claude_search` losing to the generic `search`;
`detect_anomalies_nearmiss` losing to `errors_by_service`).

So the real effect is **cross-lane competitor contamination**, not raw tool
count. This is directly relevant to your one-model-vs-per-lane question.

### 3.3 What description engineering achieved, and where it stopped

Tool descriptions were tuned against real eval misses across several rounds:

- A **reciprocal disambiguation** pattern works: adding "not X — see Y for
  that" to the *competing* tool. Measured: `mistral-saba` 86.3% → 92.2%,
  `deepseek-v4-flash` 90.2% → 94.1%, zero regressions.
- Strengthening only the *target* tool's own description measured **zero
  effect** — the competing tool's *name* (e.g. `claude_token_burn` matching
  "burning the most tokens") outweighs added text elsewhere.
- On `mistral-small` specifically, further rounds hit a ceiling: two
  iterations of stronger wording produced no improvement and one regression.
  Four residual misses remain, **all of which pass on `deepseek-v4-flash`** —
  consistent with a model-capability limit, not a wording problem.

**Interpretation offered for your consideration:** prompt/description
engineering appears to have reached diminishing returns for the 24B open
model, while a stronger model handles the same cases fine. That gap is
precisely the gap a specialised fine-tune would need to close.

### 3.4 Known collision structure (candidate training signal)

An offline analysis (`evals/tool_collisions.py`, TF-IDF over the tool
catalog) finds clusters of mutually-confusable tools. Two mechanisms:

- **Description overlap** — e.g. `claude_workflow_insights` /
  `claude_errors` / `claude_token_burn`.
- **Shared name tokens** — two tools sharing a rare word in their *name*.
  The retrieval scoring weights a name-token match 3x a description match.
  Examples: `claude_search`/`search` (shared `search`),
  `claude_workflow_insights`/`claude_efficiency_insights` (shared `insight`).

These clusters are where the residual errors concentrate. A training set that
oversamples these boundaries is likely more valuable than uniform sampling.

---

## 4. The deployment target and its hard constraints

The motivating use case is **sovereign / air-gapped deployment**: every layer
runs on hardware the operator owns, no cloud egress. berserk-mcp itself is
pure stdlib and never calls out. The LLM is the only piece that would
otherwise be an egress dependency — hence wanting a local model.

**Constraints you must plan within:**

1. **Zero third-party dependencies in the shipped server.** berserk-mcp is
   stdlib-only and this is a stated selling point. Anything requiring a new
   runtime dependency in the server itself needs explicit justification.
   (Training-time tooling is obviously exempt; inference-time is the
   constraint.)
2. **Stated local production target: Q4_K_M quantization at 8k context.**
3. **A hard, unreconciled problem:** the tool schema alone does not fit 8k
   context. Estimated schema tokens (scaled from a measured anchor of 69
   tools ≈ 27K real tokens):

   | Config | Tools | Est. schema tokens |
   |---|---|---|
   | `all` | 74 | ~29,000 |
   | `claude` | 46 | ~18,100 |
   | `sre` | 32 | ~12,100 |
   | `soc` | 31 | ~11,400 |
   | `ops` | 23 | ~8,100 |

   **Even the smallest lane consumes the entire 8k window with tool schema
   alone**, before any prompt or reply. Options previously identified: run at
   larger context (costs VRAM), use discovery mode (~1,386 tokens measured,
   but its keyword retrieval has known failure modes), or split into
   domain-scoped servers. **This constraint is a strong argument for
   per-lane or otherwise schema-reducing approaches — please address it.**

4. **Hardware is an open unknown.** ~55GB GPU RAM at bf16 for the 24B
   candidate, materially less at Q4_K_M. Whether suitable hardware exists has
   not been established. Flag where your plan depends on this.

5. **No local deployment has ever been benchmarked.** Every number in
   Section 3 comes from OpenRouter-hosted inference at that provider's
   quantization — *not* from a local deployment at Q4_K_M. The project's own
   dev brief mandates a local parity run ("nothing goes out externally
   without it") and it has never been done. Treat local behaviour as
   genuinely unmeasured.

---

## 5. The evaluation harness you would be measured against

Any proposed model must be evaluable by the existing harness — do not assume
a new one.

- **`evals/run_eval.py`** — connects to the MCP server over stdio, pulls the
  real tool list, and for each labelled prompt asks a candidate model to
  choose a tool. Records the model's **first** tool call. Backends:
  `openai` (any OpenAI-compatible endpoint, including **Ollama** and **LM
  Studio** — both already supported), `anthropic`, `mock`.
- **`evals/router_cases.jsonl`** — 54 labelled cases. Shape:

  ```json
  {"id": "top_cpu_container", "prompt": "Which container is using the most CPU right now?", "expect_tool": "top_cpu"}
  {"id": "jdive_loop", "prompt": "Did Claude Code session xyz789 get stuck in a loop?", "expect_tool": "claude_session_deep_dive", "expect_args": {"session_id": "xyz789"}, "tier": "small"}
  ```

- **`evals/run_ledger.jsonl`** — committed, append-only. One record per real
  eval run: model, backend, case file + content hash, role/tier/discovery
  config, tool count, both accuracies, **the full miss list**, cost, latency.
  This is the durable evidence base; cite records from it rather than
  remembered numbers.
- **`evals/ci_gate.py`** — CI gate, currently thresholded at 65%
  tool-selection accuracy against the mock keyword baseline.
- Local run, no cloud:
  ```bash
  python3 evals/run_eval.py evals/router_cases.jsonl --backend ollama --model <model>
  BERSERK_MCP_ROLE=sre python3 evals/run_eval.py <lane-cases>.jsonl --backend ollama --model <model>
  ```

**54 cases is small.** A serious training effort needs a much larger, held-out
evaluation set; propose how to build one (Section 6 is relevant).

---

## 6. Training data — what the Berserk cluster actually holds

This is the question with the most upside and the most uncertainty. Here is
precisely what exists, verified by reading the ingestion code.

### 6.1 What is ingested

A forwarder daemon (`claude-otel-forwarder/forwarder.py`) tails Claude Code's
session logs (`~/.claude/projects/*/*.jsonl`) and ships them to Berserk over
OTLP, tagged `service.name = "claude-code"`. Per record it sets, among
others:

- `claude.session_id`, `claude.uuid`, `claude.message_id` — allow ordering
  and pairing turns within a session
- `claude.type` — turn type (user vs assistant)
- `claude.tool_names` — **comma-joined names of tools called on that
  assistant turn**
- `claude.num_tool_uses`
- `claude.message_model` — which model produced the turn
- `claude.error`, `claude.file_targets`
- the message **body**, redacted and capped (default 16KB)

### 6.2 The key insight: real (prompt → tool) pairs are recoverable

An assistant turn carries `claude.tool_names`; the **preceding user turn's
body is the prompt that produced it**. Pairing them by `session_id` and
timestamp yields **real, in-the-wild (natural-language request → tool
selected) examples** — exactly the supervised signal a router needs, drawn
from actual usage rather than hand-authored cases.

Critically, when a Claude Code session calls a berserk-mcp tool, the tool name
appears in `claude.tool_names` in MCP form — **`mcp__berserk__<tool>`** (e.g.
`mcp__berserk__top_cpu`). So berserk-specific routing decisions are
separable from Claude Code's own built-in tools (Read, Edit, Bash…).

A starting query to size this — **run this first, the plan depends on the
answer**:

```kql
default
| where resource['service.name'] == 'claude-code'
| where tostring(attributes['claude.tool_names']) contains 'mcp__berserk__'
| summarize calls=count() by tool=tostring(attributes['claude.tool_names'])
| order by calls desc
```

### 6.3 Honest limitations — do not overstate this data

- **Volume is unknown and may be small.** berserk-mcp is one MCP server among
  many; how much it has actually been exercised is unmeasured. If the answer
  is "a few hundred calls", this is a validation set, not a training set.
- **It is unlabelled with respect to correctness.** `claude.tool_names`
  records what was *called*, not what was *right*. Frontier models produced
  most of it, so it is a reasonable weak label — but it encodes their
  mistakes too.
- **Bodies are redacted and truncated** (16KB cap, secret/high-entropy
  patterns removed). Prompts are therefore altered, sometimes materially.
- **Distribution skew.** Real usage clusters on a few popular tools; the rare
  tools where collisions actually hurt may be barely represented.
- **The data is mostly Claude Code's own tool use**, not berserk-mcp's, so
  expect heavy filtering.
- **Privacy/sovereignty.** This is operator telemetry from a homelab. Any
  training corpus derived from it inherits that sensitivity. This repo has a
  documented history of a real leak (a private hostname/IP reaching public
  GitHub, requiring a history rewrite and force-push). Treat derived datasets
  as sensitive by default and say so in your plan.

### 6.4 Other data sources worth considering

- **`evals/router_cases.jsonl`** (54 hand-labelled, high-quality, but tiny)
- **`evals/run_ledger.jsonl`** — every past eval run with full miss lists;
  a ready-made record of *which cases which models get wrong*, useful for
  targeting hard-negative mining
- **The tool catalog itself** — 74 tools with names, descriptions, and JSON
  schemas. Synthetic prompt generation from tool definitions is an obvious
  avenue; the collision clusters in Section 3.4 tell you where to concentrate
  hard negatives.
- **Canary eval results** already in the cluster under `eval.*` attributes
  (`eval.model`, `eval.tool_accuracy`, `eval.case_set_version`, `eval.role`,
  `eval.discovery_mode`, `eval.tier`).

---

## 7. Specific questions to answer

1. **Viability and size.** Given 7-8B ≈ 5-7%, ~12B = 63%, 24B = 78-80%
   untuned — where would you expect a *specialised* model to land, and at
   what size? Is the goal "make a 7B usable" (huge win if possible, given the
   8k/VRAM constraints) or "make the 24B excellent"? Justify from the cliff
   shape in the data.

2. **One model or one per lane?** The evidence cuts both ways and you should
   weigh it explicitly:
   - *For per-lane:* the entire measured penalty is cross-lane contamination;
     `ops` hit 100% at 23 tools; the 8k context budget only works with small
     schemas; published guidance suggests ~30 tools is where descriptions
     start colliding.
   - *Against per-lane:* `sre`/`soc` already score 95-96% with no scoping
     benefit at all, so per-lane specialists would add operational complexity
     for zero measured gain there; N models is N× the training, storage,
     VRAM, and maintenance; a cross-lane question would have no home.
   - Consider hybrids: one base model with per-lane LoRA adapters; a
     two-stage lane-classifier → tool-selector; or a single model trained
     *with* lane conditioning in the prompt.

3. **Training approach.** Full fine-tune vs LoRA/QLoRA vs instruction-tuning
   vs distillation from a stronger model (e.g. using `deepseek-v4-flash`,
   which solves the cases `mistral-small` fails, as a teacher). Consider also
   whether **constrained/grammar-guided decoding** over the known tool-name
   set would capture much of the gain with no training at all — the output
   space is a closed set of 74 names, which is unusually favourable.

4. **The cheapest falsifying experiment.** What is the smallest, fastest test
   that would tell us this is *not* worth pursuing? Prefer this over a plan
   that only reveals its answer at the end.

5. **Evaluation honesty.** How to build a held-out set that is not
   contaminated by the training data, given the eval cases and the telemetry
   may overlap. Note that this project's culture requires before/after
   measurement on real models with zero-regression checks, and comparing
   **miss sets, not aggregate percentages** — a real improvement was once
   masked by an unrelated case flipping by run-to-run noise.

---

## 8. Prior-art searches to run elsewhere

You have no internet access. These are queries for the operator to run from a
web-capable session, with what to look for. Please also state in your output
**which of your claims most need external validation**, so the searches can be
targeted at your actual uncertainties.

**Small-model tool calling / function calling**
- "small language model function calling accuracy tool count scaling"
- "BFCL Berkeley Function Calling Leaderboard small models limitations"
- "function calling degradation many tools context"
- "LLM tool selection accuracy number of tools threshold"

**Fine-tuning for tool use**
- "LoRA fine-tune function calling small model" / "QLoRA tool calling 7B"
- "xLAM Salesforce function calling dataset" (open function-calling corpora)
- "ToolBench ToolLLM training tool selection"
- "Gorilla LLM API call fine-tuning"
- "APIGen synthetic function calling data generation pipeline"
- "Hermes function calling dataset NousResearch"

**Distillation / teacher-student for routing**
- "knowledge distillation tool selection router small model"
- "distill frontier model tool calls into small model"

**Alternatives to training (cheaper wins to rule out first)**
- "constrained decoding function name grammar GBNF llama.cpp"
- "structured output tool selection logit bias closed set"
- "retrieval augmented tool selection embedding tool descriptions"
- "semantic tool retrieval MCP vector search"
- "intent classification vs generative tool calling comparison"

**Deployment constraints**
- "Q4_K_M quantization accuracy degradation tool calling"
- "quantization impact function calling accuracy"
- "mistral-small-3.2-24b-instruct local deployment VRAM requirements"
- "8k context window tool schema budget MCP"

**Search hygiene note:** this project has been burned by benchmark
generalisation before — BFCL ranks 7-8B families well, and they scored 5-7%
here. When evaluating any external result, check **the tool count it was
measured at**; results below ~20 tools should not be assumed to transfer.

---

## 9. Output format requested

A markdown implementation plan containing:

1. **A viability verdict up front** — go / no-go / go-with-conditions, with
   the reasoning that drives it and what would change your mind.
2. **The one-model vs per-lane recommendation**, argued from Section 3.2's
   data specifically.
3. **A phased plan**, cheapest falsifying experiment first, with explicit
   exit criteria per phase — a phase that cannot fail is not a phase.
4. **Data pipeline design** — what to extract from the cluster, how to
   filter/label/deduplicate it, how to build held-out splits, how to handle
   its sensitivity.
5. **Concrete resource estimates** where possible, flagged as estimates, and
   flagged explicitly where the unknown hardware answer changes the plan.
6. **What you would NOT do, and why** — this project values ruling things out
   explicitly (e.g. it has already ruled out fine-tuning-for-disambiguation
   and clarification-question loops as conflicting with its "any model, no
   training, one-shot routing" premise; note that a *deliberately trained*
   model changes that calculus, and say so if you think it does).
7. **An explicit list of your assumptions and unknowns**, separated from your
   conclusions.

Please keep measured / estimated / unknown clearly distinguished throughout,
as this document does.

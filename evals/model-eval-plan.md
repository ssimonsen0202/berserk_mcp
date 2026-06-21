# Model evaluation plan — which LLM to pair with berserk-mcp

Goal: find the **cheapest model (ideally local) that reliably drives this MCP** for the
real question distribution, and quantify how much the server design (tool descriptions,
`instructions`, annotations) contributes versus the model.

This is a living plan. It has two parts: (1) what public data already tells us and how
to read it, and (2) a concrete, model-agnostic test harness to run ourselves.

---

## Part 1 — What public benchmark data exists (and why it's only a starting point)

There is good public data on model *tool-calling*, but **none of it measures your
server**. Use it to shortlist candidates, not to pick the winner.

### The benchmarks that matter

| Benchmark | What it measures | How it maps to berserk-mcp |
|---|---|---|
| **BFCL** (Berkeley Function-Calling Leaderboard) | Raw function-calling: does the model pick the right function and format args correctly (AST-checked)? | **Best proxy for us.** Our hard part is exactly "pick the right tool + a valid `since`." |
| **MCP-Universe** (Salesforce) | End-to-end agents on 11 *real* MCP servers, 231 long-horizon multi-server tasks | Upper bound on difficulty. Our server is far easier (mostly one tool call), so its low absolute scores **do not** predict our success rate. |
| **MCP-Atlas** | Tool-use competency in environments with *many* tools | Relevant to "does adding tools degrade routing?" — useful if we grow past ~20 tools. |

### What the numbers say (as of mid-2026 — re-check before deciding)

- **BFCL v3:** top scores cluster ~75–77% — GLM 4.5 (76.7%), Qwen3 32B (75.7%). Among
  locally-runnable models, the **Qwen** family and **Llama 3.1** line score well
  (Llama 3.1 8B ≈ 0.76, 70B ≈ 0.85; Qwen3 32B ≈ 0.70). This is the single most useful
  list for shortlisting a local model.
- **MCP-Universe:** even the best model (GPT-5) only reaches ~44% overall; Gemini-3-Pro
  ~44.6% on the function-calls track, Claude-4.5-Sonnet ~35%, best *open* model
  (GPT-OSS-120B) ~25.5%. **Read this as: hard multi-server agentics is unsolved — not as
  "open models can't do MCP."** Our server requires none of that long-horizon chaining.

### The honest conclusion

Public leaderboards rank *general* tool-calling ability and *hard* multi-server agentics.
berserk-mcp sits at the easy end of the spectrum **by design**: 19 well-named tools, the
model picks one tool + a time window, and never authors KQL. So:

1. Use **BFCL** to pick which 4–6 models to try (favor Qwen2.5/Qwen3, Llama 3.1+).
2. Ignore MCP-Universe's low absolute numbers as a go/no-go for local models.
3. **Run our own eval** — below — because difficulty profile, prompt, and the cost
   bars are ours, not the benchmark's.

Sources are listed at the bottom.

---

## Part 2 — Our test harness

### What "best experience" means (define the bar first)

A model is "good enough to pair with berserk-mcp" if it clears all of:

| Metric | Bar | Why |
|---|---|---|
| **Tool-selection accuracy** | ≥ 95% | Picking the wrong tool is the dominant failure; this is the whole value prop. |
| **Argument correctness** | ≥ 90% | Right `since` window / service name / search term. |
| **End-to-end task success** | ≥ 90% | Correct final answer (string-compared to ground truth). |
| **Median turns** | ≤ 2 for single-fact Qs | Loops = wasted tokens/latency and a bad UX signal. |
| **Invalid/hallucinated tool rate** | ~0% | Calling a nonexistent tool, or `search` when a dedicated tool existed. |
| **Latency (p95)** | depends on use case | ChatOps reply: < ~5 s. Unattended cron digest: minutes OK. |
| **Cost** | as low as possible | Local = ~free; report $/task for API models. |

"Best" = the **cheapest model (local preferred) that clears the bar**, not the top scorer.

### Two layers (run Layer A first — it's cheap and high-signal)

**Layer A — Router eval (no live Berserk needed).**
A fixed set of ~40 natural-language prompts, each labelled with the *expected* tool name
and key args. The harness sends the prompt + the MCP tool list to a candidate model,
captures the **first tool call**, and scores tool + arg match by exact comparison. Tool
*execution* is stubbed (return a canned table), so this runs fast, offline, repeatable,
and isolates routing from data-reading. This is the highest-value, lowest-cost test and
directly targets what the MCP needs.

**Layer B — End-to-end eval (needs a live Berserk with a frozen time window).**
~10 Q&A pairs in the [mcp-builder XML format](#layer-b-question-format), with **stable,
verifiable** answers derived from a fixed historical window of real data (so answers
don't drift). The harness runs the full agent loop — model calls real tools, reads real
output, produces a final answer — scored by string comparison. This measures the whole
experience, including digesting tool output.

### Candidate matrix (local-first, cost-ordered)

| Tier | Models | Notes |
|---|---|---|
| **Local (preferred)** | Qwen2.5-7B-Instruct, Qwen2.5-14B-Instruct, Llama-3.1-8B-Instruct, Mistral-Small, Qwen3-8B | 7B Q4 ≈ 5–6 GB (fits RTX 3080 10 GB); 14B Q4 ≈ 9 GB (tight on 3080, OK on Mac 16 GB). All have native tool-calling in Ollama. |
| **Local floor check** | qwen3:1.7b (the homelab default) | Confirm the lower bound. Known CPU-bound on the 2-vCPU VM; test on the GPU box to separate "model can't route" from "hardware too slow." |
| **Cheap API** | gpt-4.1-mini, Claude Haiku, Gemini Flash | Fallback when local won't fit or latency matters. |
| **Frontier (ceiling)** | Claude Sonnet, GPT-5-class | Reference only — establishes the gap, not a deployment target. |

### Harness architecture (model-agnostic)

One runner, swap `base_url` + `model` + key per backend. Use the **OpenAI-compatible
chat-completions + tools API** as the universal surface — Ollama (`/v1`), OpenAI, Gemini
(openai-compat), and Anthropic (openai-compat endpoint) all speak it, so one client
covers every candidate.

```
for each model in matrix:
  for each question (× N repeats, N≥3 — tool-calling is stochastic):
    1. tools = MCP tools/list  →  convert to OpenAI tool schema
       (inputSchema is already JSON Schema → drop-in)
    2. loop (max_turns=6):
         resp = chat(model, messages, tools)
         if resp has tool_call:
             Layer A: record tool+args, stop (stubbed execution)
             Layer B: execute via MCP stdio, append result, continue
         else: record final answer, stop
    3. log: chosen tool(s), args, #turns, latency, prompt/completion tokens, answer
  aggregate: success rate + variance, median turns, p95 latency, $/task
emit: model × metric comparison table (CSV + Markdown)
```

Run each (model × question) **N≥3 times** and report mean ± spread — small models are
flaky and a single run misleads.

### Bonus: A/B the *server*, not just the model

Re-run the same mid-tier model (e.g. Qwen2.5-7B) under variants to quantify the design's
contribution and guard against regressions when editing tool docs:
- with vs. without the server `instructions`
- terse vs. verbose tool descriptions
- with vs. without tool `title`s

This tells us how much of the reliability is the *server* (portable to any model) vs. the
model — and catches description edits that quietly hurt routing.

### Reuse what we already have

- The 26-test offline suite already stubs `bzrk`; the Layer-A harness reuses that stub
  pattern, so it needs no live backend.
- The homelab already ships telemetry (incl. cost metrics like `hermes.api.cost_usd`)
  into Berserk — the harness can optionally emit its own run metrics there too, but a
  local CSV/JSON is enough to start.

### <a name="layer-b-question-format"></a>Layer B question format (mcp-builder XML)

```xml
<evaluation>
  <qa_pair>
    <question>In the 24h window ending 2026-06-20T00:00Z, which service logged the most ERROR lines? Give the service name.</question>
    <answer>janitor</answer>
  </qa_pair>
</evaluation>
```
Answers must be **stable** (frozen window), **single-value**, and **string-comparable**.

---

## Next steps

1. Build the runner (`evals/run_eval.py`) — OpenAI-compatible client + MCP stdio loop + scoring.
2. Write Layer-A `evals/router_cases.jsonl` (~40 labelled prompts) — can be done now, no backend.
3. Pick a frozen Berserk window and author 10 Layer-B Q&A pairs with verified answers.
4. Run the matrix; publish the comparison table in this file.

## Results — live agent eval (2026-06-21)

Tested the **deployed Hermes agent** on VM-A (the real use case) driving the live MCP
against live Berserk, comparing the default cheap lane (`gpt-4.1-mini`) against the
`@deep` lane (`claude-sonnet-4-6`). Each question was run via `hermes -z`, and answers
were verified against the deterministic `bzrk-q` helper as ground truth.

> Caveat: this exercised the *deployed two-file MCP* (`/opt/assistant/bin/`), not the
> public single-file rewrite. KQL is identical, but the rewrite's server `instructions`
> + tool annotations were **not** deployed — so this measures the older tool descriptions.

| Question class | Right tool | gpt-4.1-mini | claude-sonnet-4-6 | Ground truth | Result |
|---|---|---|---|---|---|
| Errors, last 1h | `errors_by_service` | "none" ✓ | "none" ✓ | empty ✓ | tie |
| Top-3 CPU containers | `top_cpu` | minio→query→postgres ✓ | same ✓ | minio→postgres→query | tie (postgres/query a noise-tie) |
| Heaviest **VM** (host-vs-container trap) | `host_cpu` | beserk 1.49 ✓ | beserk 1.52 ✓ | beserk 1.45 ✓ | **tie — cheap passed the trap** |
| Multi-step: busiest VM == host of top container? | `host_cpu` + `top_cpu` | beserk + minio, correct join ✓ | (not run) | beserk 1.45 / minio 4.9% | **cheap passed the hardest test** |

### Verdict

No measurable gap between `gpt-4.1-mini` and `claude-sonnet-4-6` on monitoring / ChatOps
questions — including the host-vs-container disambiguation and a two-tool synthesis that
usually separates cheap models. **Default to `gpt-4.1-mini`; keep `@deep`/sonnet as an
on-demand escape hatch** for genuinely hard asks. The deterministic tool descriptions are
what carry the cheap model — which validates the MCP's core thesis.

### Methodology lessons (banked from this run)

- **Never string-compare exact values on live/averaged metrics.** `top_cpu`/`host_cpu`
  average over a window, so numbers drift run-to-run (we saw beserk vary 1.45–1.55).
  Score the *set* and clearly-separated ranks, not near-ties or absolute values. Layer-B
  questions must target stable, clearly-separated answers (or frozen windows).
- **Tool *selection* is the thing to test, not output reading** — both models read load
  averages and percentages correctly every time; the only risk was picking the wrong
  tool, which is exactly what the Layer-A router eval isolates cheaply.

### MCP gap noted

The multi-step join ("is the busiest VM also running the top container?") required mapping
a container to its host — which **no current tool exposes**. The model inferred it from
the `berserk-*` naming (correct here, not guaranteed). A future `container_hosts` tool, or
adding `host` to `top_cpu` output, would make such joins reliable.

### Follow-ups

- Deploy the improved single-file MCP (server `instructions` + annotations) to VM-A and
  re-run this set — measures whether those additions change cheap-model behavior (the
  built-in A/B from the plan above).
- Run the Layer-A router harness against prospective **local** models (Qwen2.5-7B etc.)
  on a GPU box to find a free local lane that matches `gpt-4.1-mini` here.

## Sources

- [Berkeley Function-Calling Leaderboard (BFCL) — Gorilla/UC Berkeley](https://gorilla.cs.berkeley.edu/blogs/8_berkeley_function_calling_leaderboard.html)
- [BFCL v3 scores (pricepertoken)](https://pricepertoken.com/leaderboards/benchmark/bfcl-v3) · [BFCL on llm-stats](https://llm-stats.com/benchmarks/bfcl)
- [MCP-Universe — site](https://mcp-universe.github.io/) · [paper (arXiv 2508.14704)](https://arxiv.org/abs/2508.14704) · [Salesforce repo](https://github.com/SalesforceAIResearch/MCP-Universe)
- [MCP-Atlas: Large-Scale Benchmark for Tool-Use Competency with Real MCP Servers (arXiv 2602.00933)](https://arxiv.org/abs/2602.00933)

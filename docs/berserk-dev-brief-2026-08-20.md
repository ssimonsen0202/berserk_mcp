> **Correction, 2026-08-20.** Two items in §5's backlog table are already
> shipped, ahead of this brief:
>
> - **Item 4, result envelope** ("echo resolved window, disambiguate `(no
>   rows)`, degrade overflow to a marked summary") — this is
>   [issue #2](https://github.com/ssimonsen0202/berserk_mcp/issues/2),
>   merged as [PR #10](https://github.com/ssimonsen0202/berserk_mcp/pull/10).
>   Spec: `docs/result-envelope-implementation-spec.md`.
> - **The deferred note on saved-query projection** ("do it after discovery,
>   so packs land in a discovery surface rather than a flat list") — this is
>   [issue #5](https://github.com/ssimonsen0202/berserk_mcp/issues/5), merged
>   as [PR #9](https://github.com/ssimonsen0202/berserk_mcp/pull/9). It
>   shipped *before* discovery, the reverse of the brief's recommended
>   order — worth the team's attention if that ordering assumption fed into
>   anything else in §5 or the referenced backlog doc.
>
> The rest of the analysis (market landscape, scope rationale, local-model
> reliability framing, eval methodology) is unaffected. Original text below
> is unedited.

> **Update, 2026-08-23.** §4's core question has a first real answer, not
> just a proposed methodology anymore — a real-model eval sweep ran
> 2026-08-22/23 (local Ollama + OpenRouter, 8 models, this repo's own
> 69-tool schema). Full data and sourcing:
> [`model-routing-cost-validation-2026-08-23.md`](model-routing-cost-validation-2026-08-23.md).
> Headline: the reliability floor in the data is the **24B parameter
> class**, not sub-14B as the literature review below implied might be
> viable with enough harness support — a 12B model (mistral-nemo) scored
> *below* the dumb keyword-matching baseline, while 24B-class models
> cleared it comfortably. See the new subsection at the end of §4 for the
> full table and, importantly, which of this section's own three
> measurement guardrails that data does and doesn't satisfy — it's real
> signal, not yet the rigorous sweep this brief specifies.
>
> Also shipped since this brief: **issue #37**, per-case token cost and
> cache-hit-rate now persist in eval results (directly feeds the
> "cost-per-correct-answer per rung" metric §4 asks for), and **issue
> #42**, multi-agent telemetry ingestion — Codex CLI is now a second
> ingested agent alongside Claude Code. This doesn't touch the Berserk-only
> *backend* scope constraint in §2 (still one backend, bzrk); it's
> orthogonal — more agent *sources* feeding the same single backend, not a
> second backend. Tool count is 69 as of this update (was 59 when this
> brief was written) — worth naming explicitly since §4(a) argues surface
> size is the dominant lever on small-model reliability; the growth is in
> the FinOps/economics lane, not the router-critical path, but it's still
> more schema on every call.
>
> **Connecting this to the investor-facing read** —
> [`investor-brief-berserk-mcp-2026-08-23.md`](investor-brief-berserk-mcp-2026-08-23.md)
> covers the same competitive ground as §1 below independently, and lands on the
> same facts (Datadog MCP GA March 2026, Grafana MCP early 2026) — worth treating
> as cross-validation, not coincidence. But it surfaces a differentiator this
> brief's §2/§3 don't name as one: fixed-queries-for-reproducibility and
> local-for-sovereignty are the two pillars argued below, and they're both still
> right, but the `claude_*` lane's cost/spend economics — largely built already,
> ahead of any explicit roadmap item here — is a *third* one, positioned against
> the exact same incumbents named in §1. Their MCP servers expose infra data
> outward to agents; ours does that *and* turns around and reports what the
> agents themselves are costing. Worth deciding whether that belongs as an
> explicit pillar in this brief's own framing, not just the investor one — this
> doc should keep evolving as both sides of the market/engineering read sharpen
> each other, not stay fixed as of any one date.

# Berserk MCP — where the market went, and where we sit

Short brief for the Berserk devs. Two questions:

1. What happened in the observability-agent market, and does our design still make sense?
2. Can a locally hosted non-frontier model actually drive this thing reliably?

**Scope constraint:** `berserk_mcp` targets Berserk/bzrk only. No multi-backend adapter,
no Sentinel, no generic OTel. Everything below is written inside that boundary. Full
detail, issue backlog and review rubric in `berserk-mcp-strategy-and-backlog.md`.

---

## 1. What changed in the last 12 months

**Our category went from novel to crowded.** Nine vendors now ship official MCP servers —
Datadog, Grafana, Splunk, Dynatrace, Elastic, Sentry, Honeycomb, PagerDuty, Instana. The
highest first-party adoption rate of any MCP category. Datadog's is GA at ~150 tools
across 20 toolsets. Grafana Cloud MCP went GA at their July 2026 AI Week.

**It's validated at hyperscaler scale.** Azure SRE Agent and AWS DevOps Agent both hit GA
in April 2026, and both use observability MCP servers as their primary investigation
interface. The pattern we bet on is now the industry default.

**Four things are on everyone's roadmap:** agentic multi-step investigation, a headless
CLI lane beside the MCP lane (Datadog Pup, Grafana `gcx`), agent/AI observability as its
own product line, and telemetry cost control.

**But the AI-SRE wave has not delivered.** A synthesis of 27 papers across 19 benchmarks
found agents scoring 92% on tool-use sub-tasks and 34% end-to-end. Field reporting says
most of it made teams faster at *correlating* signals, not at *isolating root cause*. At
least one large survey found AI coincided with more delivery instability and more toil.

**Sovereignty is a genuine tailwind.** EDPB named on-premise inference the strongest
available LLM data-protection mitigation. Gartner puts sovereign cloud IaaS at $80B in
2026, +35.6%, with 75% of enterprises holding a sovereignty strategy by 2030.

---

## 2. What the Berserk-only scope means

Worth being explicit, because it changes the competitive read.

**We are not competing for the general observability-agent market.** Every vendor MCP
server is single-backend too — Datadog's speaks Datadog, Grafana's speaks Grafana. The
difference is that their backend has a large installed base and ours doesn't. So the
honest framing is not "how do we win the category" but **"we are the access layer for
Berserk deployments, and it should be the best one that exists for any backend."**

That constraint costs us reach. It also buys three things:

**1. The sovereignty claim becomes unconditional.** Every adapter target we considered
would have spent it — Sentinel puts telemetry in Microsoft's cloud, hosted OTel in a
vendor's. Staying bzrk-only means *nothing leaves the building*, full stop, with no
deployment-dependent asterisk. That is the strongest single claim we have and it is now
clean.

**2. Reproducibility stays simple.** `kql_canonical_sha256` is KQL-specific. One query
language means one canonical form, one hash, one replay path — no cross-backend
provenance abstraction to design or defend.

**3. Engineering focus.** The adapter work was the largest, riskiest epic on the board.
Removing it puts everything behind reliability and safety, which is where the actual
differentiation is.

**The strategic risk to name out loud:** our value is now tightly coupled to Berserk
adoption. If Berserk grows, the MCP server compounds with it. If it doesn't, no amount of
MCP quality compensates. That's a Berserk-level bet, not an MCP-level one, and it's not
ours to make — but we should build as if the answer is yes and make the MCP a reason to
choose Berserk rather than an afterthought to it.

---

## 3. Does our design still make sense?

Yes, and for reasons that got *stronger*.

**Fixed queries.** Everyone else lets the model author the query language. OpenObserve
translates NL→SQL/PromQL. Datadog ships `ddsql_run_query`, `build_audit_trail_query`, and
now `execute_code` — agent-authored TypeScript in a sandbox. All non-reproducible by
construction. We can prove *what the agent asked*, not just report what it found. Nobody
else can.

**Local by default.** Most vendor MCP servers are remote-hosted. Your telemetry *and the
questions your engineers ask about it* leave the perimeter, and the audit trail of AI
access to production lives in the vendor's cloud. OpenObserve is self-hostable but gates
RBAC and audit behind the enterprise tier — their own docs state the open-source edition
has no RBAC at all. We can ship both in the open path.

**Small tool surface.** 59 tools vs Datadog's ~150. This turns out to decide whether a
local model works at all — see §4.

### Two real weaknesses

**Composition ceiling.** Datadog shipped `execute_code` specifically because fixed tools
can't compose across signals. That's the strongest evidence available that a pure
fixed-query surface has a limit. Our answer should be a *fixed decision tree* investigation
tool — not code execution — but we shouldn't pretend the ceiling isn't there.

**A security hole in the middle of our own argument.** We return raw log lines to the model
unfenced. Anyone who can write a log line an agent later reads can inject into its context.
Datadog fences far less exposed surfaces than that (warehouse SQL text, form responses)
with explicit data-not-instructions markers. Cheap to fix, embarrassing not to.

### One correction worth internalising

An earlier draft claimed nobody attempts *wrong-answer* containment — stopping the model
confidently reporting a false negative. Wrong. Datadog does it in their experiments
toolset: an authoritative `verdict` field the model is explicitly forbidden to recompute,
plus a diagnostics tool that declares when results are not safe to interpret at all.

The narrower claim survives: **nobody applies it to the log and metric query path**, which
is where 4am false negatives happen. Treat their pattern as prior art to copy, not a gap
to exploit.

---

## 4. Can a local non-frontier model drive this reliably?

The interesting question. Honest answer: **we don't know yet, and the target we've been
quoting measures the wrong thing.**

### The 7B target is more ambitious than we've treated it

A 2026 study across 1,980 deterministic tool-invocation tests found capacity thresholds at
**14B (minimum viable, 96.6–97.4% success)** and **32B (parity with closed models)**.
Sub-14B needed "substantial architectural augmentation." A 3B model showed 89% error rates
on tool *initialization* alone — not selection, initialization.

Our fixed-query design **is** that architectural augmentation. So the interesting claim
isn't "7B works." It's: *here is the curve, and here is how many billions of parameters the
harness is worth.* Better engineering result, and a publishable one.

### Three things the market data tells us

**(a) Tool-surface size is the dominant lever, and it's measurable.** Each MCP tool
definition costs 200–500 tokens. Five servers × 30 tools burns 30–60k tokens before the
first user message. When Copilot cut from 40 tools to 13 they measured ~400ms lower average
latency, 190ms off time-to-first-token, *and* +2–5pp on benchmarks — simultaneously.
Smaller surface is not a trade-off against quality; it *is* quality.

Our per-lane surface is ~3,500 tokens today. Just-in-time discovery (~8 resident anchors +
a `find_tool` search) is the highest-leverage change available to us. OpenObserve already
ships this; Datadog's toolsets are the blunter version.

**(b) Per-call accuracy is not per-question accuracy.** Errors compound multiplicatively.
95% per call across 10 hops is 59%; across 20 hops, 36%. Fixed queries help enormously
because most of our questions are one call.

**But discovery makes every question two hops.** 95% recall × 95% selection = 90% per
question — *worse than today's single hop*. That's why the discovery recall gate is ≥99% at
K≤5, not 95%. And it's why anything that *removes* a hop beats anything that improves one:
a 99% hop deleted beats a 95% hop raised to 99%.

Which is why the best idea in the Datadog teardown is `recommended_tool_call` — their tools
return the next call *with parameters pre-filled*, turning a routing decision into a
parameter-passing operation. Copy it, but populated from a **static table**, never
generated — a generated hint is an LLM routing decision wearing a deterministic costume,
and it would forfeit the reproducibility claim.

**(c) 95% is the dangerous part of the trust curve, not the safe part.** The floor is
settled: below ~70% reliability, automation makes human performance *worse*. But the
ceiling behaviour is the problem — studies of "very reliable but not perfectly reliable"
aids found operators performed worse than with no automation at all, commission errors
approaching 100%. Small errors disproportionately destroy trust while operators become
*less* sensitive to large ones.

At 85% an on-call engineer keeps verifying. At 95% they stop, and the 5% arrives
unannounced at 04:00.

### So we're changing what we measure

Old: ≥95% tool selection, ≥90% argument correctness.

| Metric | Target |
|---|---|
| **Per-question success** (end-to-end, all hops — not per-call) | ≥90% |
| **Silent-failure rate** — plausible, well-formatted, undetectable without independent verification | **≤0.5% — the only hard gate** |
| Loud-failure rate — obviously wrong, engineer re-asks | ≤8% |
| Discovery recall @ K≤5 | ≥99% |

Aggregate accuracy is nearly useless as a safety signal because it merges two failure
classes with wildly different costs. A wrong tool returning obviously irrelevant data costs
two seconds. `top_cpu` when the engineer meant `host_cpu` — plausible, well formatted,
wrong — is the one that compounds, and automation bias makes it invisible.

### How we find out

An eval harness over OpenRouter: one OpenAI-compatible endpoint, 400+ models, so the same
case file runs 3B through frontier without per-provider plumbing. Sweep a ladder
(3B/7B/8B/14B/32B/70B + a frontier control) against a harness-feature ablation matrix —
discovery on/off, `since` normalizer on/off, envelope on/off, tier gating on/off,
`next_call` hints on/off.

The output we want: **"the `since` normalizer is worth ~7B parameters."** Harness value
denominated in model size, plus cost-per-correct-answer per rung, which is what actually
determines where the two-tier boundary belongs.

Three guardrails or the measurement is invalid:

- `provider.require_parameters: true` — otherwise you get routed to an endpoint that
  silently ignores `tools` and returns a perfectly valid-looking wrong measurement.
- **Pin the provider, disable fallbacks.** Same model slug, different provider, different
  quantization. A sweep where the provider changed mid-run isn't a measurement of the
  model.
- **Synthetic fixtures only.** Eval prompts leave the perimeter. A sovereignty product
  whose test suite exfiltrates real hostnames would be the worst own-goal available to us.
  Blocking CI check.

Plus a parity step: OpenRouter tells us how a model behaves at *some provider's*
quantization. Production is Q4_K_M at 8k context on our own hardware. Re-run the top rungs
locally and publish the delta. **Nothing goes out externally without it.** OpenRouter is a
measurement instrument, never a runtime dependency — nothing in the shipped server imports
it.

### First real data (2026-08-22/23) — a data point, not yet the sweep above

Ran the existing eval harness (`evals/run_eval.py`, already OpenRouter-capable) across 8
models — 2 local (Ollama), 6 cloud — against the current 41-case router set and this repo's
real 69-tool schema. Full table, methodology, and cost/caching analysis:
[`model-routing-cost-validation-2026-08-23.md`](model-routing-cost-validation-2026-08-23.md).
Headline numbers:

| Model | Class | Tool-selection | vs. baseline (65.9%) |
|---|---|---|---|
| Qwen2.5:7b / Llama3.1:8b (local) | ~7-8B | 5-7% | Fails badly |
| mistral-nemo | ~12B | 63% | **Fails** — below the dumb keyword matcher |
| mistral-saba | ~24B | 83% | Clears it |
| mistral-small-3.2-24b-instruct (open-weight) | ~24B | 78% | Clears it |
| deepseek-v4-flash | 284B MoE, ~13B active | 88% | Clears it well |
| deepseek-chat | undisclosed | 93% | Clears it well |

This is real signal, directionally consistent with the 14B/32B literature thresholds cited
above (the floor we measured, 24B, sits between them) — but it does **not** yet satisfy this
section's own bar, and that gap should stay visible rather than get rounded off:

- **Metric mismatch.** This measured aggregate tool-selection accuracy, the exact single
  number §4(c) argues is "nearly useless as a safety signal because it merges two failure
  classes." No silent-failure-rate / per-question-success split was computed. The 24B-floor
  finding is real, but we don't yet know what fraction of that floor's remaining ~15-20%
  error is loud-and-obvious versus silent-and-plausible — which is the number that actually
  matters for the 04:00 on-call scenario this section opens with.
- **Guardrails: 1 of 3.** Synthetic fixtures only — satisfied, no real hostnames/customer
  data in the case file. `provider.require_parameters` — **not set**; tool calls came back
  correctly-shaped across every model tested, which is weak evidence the parameter passthrough
  worked, but it was never explicitly forced, so a silently-ignored-tools failure mode can't be
  fully ruled out. Provider pinning — **not done**; OpenRouter's default routing was used, so
  quantization could have varied call-to-call for a given model slug.
- **No local-quantization parity step yet.** The winning models above were measured at
  OpenRouter's hosted quantization only. Mistral Small 3.2 24B is the one open-weight,
  genuinely self-hostable candidate in the table — it hasn't actually been run locally yet to
  check the delta this section calls mandatory before anything ships externally.

Net: real progress on the question, not a closed one. Issues #22 (routing reliability
analyzer) and #23 (OpenRouter model-ladder sweep with guardrails) — both still open — are
where the rest of this section's bar gets met.

---

## 5. What this means for what we build

Measurement first. We have 31 synthetic router cases covering 21 of 59 tools and no eval
running in CI, so everything else would ship blind.

| Order | Work | Why | Status (2026-08-23) |
|---|---|---|---|
| 1 | Eval expansion + OpenRouter backend + telemetry capture | No baseline today. Nothing below is verifiable without it | **Partial.** CI-wiring done (#13, closed). Cost/cache telemetry persists (#37, closed). Full model-ladder sweep with all 3 guardrails still open — #22, #23 |
| 2 | **Wrong-answer containment** — silent-failure taxonomy, one control per class, CI gate ≤0.5% | The only hard reliability gate. Copy Datadog's verdict/uninterpretability shapes | **Done** — #12, closed |
| 3 | Fence telemetry rows as untrusted data | Security. Small. Currently a hole in our own argument | **Done** — #11, closed |
| 4 | Result envelope — echo resolved window, disambiguate `(no rows)`, degrade overflow to a marked summary | 24 tools return raw output with no window echo. Attacks silent failure directly | **Done** — see correction block at top of this doc |
| 5 | Principal refactor (role per-request, no behaviour change) | Blocks discovery, tiers and the ledger. Do it once | Open — #16 |
| 6 | JIT discovery + `next_call` hints | The dominant lever on small-model reliability | Open — #14 |
| 7 | Audit ledger + scoped principals | Clearest differentiator; also becomes the real eval corpus | Open — #17 |
| 8 | Investigation decision tree | Answers the composition ceiling without code execution | Open — #24, explicitly needs a design decision before starting |

Deferred, not cancelled: saved-query projection into `tools/list` (do it after discovery,
so packs land in a discovery surface rather than a flat list) and the coverage-gap tool.

**One open decision for the team:** does the two-tier design survive the ladder? If 14B
single-tier beats a 7B/32B split on both cost-per-correct-answer and silent-failure rate,
that simplifies the product considerably. Worth not defending the current design until the
sweep has run.

---

## The one-paragraph version

The category filled up fast, and the incumbents are shipping enormous remote-hosted tool
surfaces that let the model author queries — so their answers aren't reproducible and their
audit trails live in their cloud. Our bet on fixed queries and locality got stronger, not
weaker, and staying Berserk-only makes the "nothing leaves your building" claim
unconditional rather than deployment-dependent. That caps our reach to Berserk deployments,
which is a Berserk-level bet rather than an MCP-level one; inside that boundary the goal is
to be the best access layer that exists for any backend. On the local-model question, the
published evidence says sub-14B needs serious harness support — which is exactly what we've
built — so the honest goal is to measure how far down the ladder the harness takes us
rather than assert 7B works. The lever that matters most is shrinking the tool surface and
deleting routing hops; the metric that matters most is not accuracy but how often we're
wrong in a way the engineer can't see.

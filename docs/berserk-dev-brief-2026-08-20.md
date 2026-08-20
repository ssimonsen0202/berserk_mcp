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

---

## 5. What this means for what we build

Measurement first. We have 31 synthetic router cases covering 21 of 59 tools and no eval
running in CI, so everything else would ship blind.

| Order | Work | Why |
|---|---|---|
| 1 | Eval expansion + OpenRouter backend + telemetry capture | No baseline today. Nothing below is verifiable without it |
| 2 | **Wrong-answer containment** — silent-failure taxonomy, one control per class, CI gate ≤0.5% | The only hard reliability gate. Copy Datadog's verdict/uninterpretability shapes |
| 3 | Fence telemetry rows as untrusted data | Security. Small. Currently a hole in our own argument |
| 4 | Result envelope — echo resolved window, disambiguate `(no rows)`, degrade overflow to a marked summary | 24 tools return raw output with no window echo. Attacks silent failure directly |
| 5 | Principal refactor (role per-request, no behaviour change) | Blocks discovery, tiers and the ledger. Do it once |
| 6 | JIT discovery + `next_call` hints | The dominant lever on small-model reliability |
| 7 | Audit ledger + scoped principals | Clearest differentiator; also becomes the real eval corpus |
| 8 | Investigation decision tree | Answers the composition ceiling without code execution |

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

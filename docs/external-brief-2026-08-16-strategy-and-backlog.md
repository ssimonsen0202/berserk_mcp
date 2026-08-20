# berserk_mcp — Product Strategy & Implementation Backlog

**Status:** draft for team review
**Baseline commit analysed:** `da85ac6` (v1.25.1)
**Market research date:** 2026-08-16
**Audience:** Berserk team; implementation by Claude Sonnet, review by Codex

> **Line references** in this document are against `berserk_mcp.py` at `da85ac6`. The
> file is 3,794 lines and moves. **Re-verify every line number by grepping for the
> named symbol before editing.** If a reference does not resolve, the symbol name is
> authoritative, not the number.

---

# Part 1 — Where we stand

## 1.1 The product thesis (from the README)

Three commitments. Every item in this backlog either serves one or is out of scope.

1. **Fixed queries so weak models route reliably.** The model picks an intent and a
   window. It never authors KQL.
2. **Conclusions, not row dumps.** A verdict, a baseline deviation, a cost trend.
3. **Sovereign two-tier local.** A small open-weight model handles ≥80% of
   interactions, escalating to a larger local model for `@deep` work.
   Targets in `evals/model-eval-plan.md` Part 3: ≥95% tool selection, ≥90% argument
   correctness, ≤20% escalation.

## 1.2 Measured baseline at `da85ac6`

| Metric | Value |
|---|---|
| Tools in `TOOLS` | 59 |
| Tools exposing `since` | 51 (86%) |
| Tools with a schema `pattern` | 1 (`claude_record_recommendation_decision`) |
| Tools on the `SIMPLE` raw-passthrough path | 24 |
| Env vars read across modules | 42 |
| Env vars documented in `.env.example` | 11 |
| Router eval cases | 31, covering 21 of 59 tools |
| CI eval invocation | none (`run_eval.py` never runs in CI) |

Per-lane tool surface:

| Lane | Tools | ~tokens in `tools/list` |
|---|---|---|
| ops | 25 | ~2,600 |
| sre | 33 | ~3,450 |
| soc | 33 | ~3,470 |
| claude | 45 | ~4,740 |

---

# Part 2 — Competitive landscape

## 2.1 What changed in the last 12 months

The relevant market is **not** telemetry platforms. It is the **access layer**: how an
agent gets trustworthy answers out of a telemetry store. That layer went from novel to
contested inside a year.

- **Nine vendors now ship official MCP servers** — the highest first-party adoption
  rate of any MCP category. Datadog GA, Grafana Cloud MCP GA (AI Week, July 2026),
  Splunk GA, Dynatrace remote, Sentry, Honeycomb, PagerDuty, Instana, Elastic.
  ([ChatForest review](https://chatforest.com/reviews/monitoring-observability-mcp-servers/))
- **Azure SRE Agent and AWS DevOps Agent both reached GA in April 2026**, and both
  consume observability MCP servers as their primary investigation interface. The
  pattern is validated at hyperscaler scale.
- **A headless CLI lane now ships alongside the MCP lane.** Datadog Pup CLI (scripting,
  automation, high-scale custom agents) and Grafana `gcx`.
- **Agentic investigation is the universal roadmap item.** Grafana Assistant
  Investigations, Rootly AI SRE, Traversal, OpenObserve AI SRE.

## 2.2 Where incumbents underdeliver

This is the exploitable part.

| Gap | Evidence |
|---|---|
| **Context bloat is measured and severe** | Each MCP tool definition costs 200–500 tokens; 5 servers × 30 tools burns 30–60k tokens before the first user message. Copilot cutting 40→13 tools produced ~400ms lower average latency, 190ms off TTFT, and +2–5pp on SWE-Lancer / SWEbench-Verified. ([Albato](https://albato.com/blog/publications/embedded-mcp-context-bloat-hallucinations)) |
| **The AI SRE wave has not delivered** | Most of it made teams faster at *correlating* signals, not at *isolating* root cause; first-wave AI has not bought back SRE toil. At least one large survey found AI coincided with more delivery instability and more toil. ([Traversal](https://www.traversal.com/blog/ai-in-incident-response-state-of-the-field-2026-sre), [KodeKloud](https://kodekloud.com/blog/ai-in-sre-whats-changing/)) |
| **Trust is the stated adoption bottleneck** | Engineers will not act on a black box. Value comes from showing hypotheses tested and eliminated with specific evidence, so an engineer verifies in seconds instead of re-investigating. ([Resolve.ai](https://resolve.ai/glossary/what-is-ai-sre)) |
| **Complexity, not capability, is the top complaint** | 39% cite complexity and operational overhead as their single biggest obstacle; orgs juggle ~8 observability tools; cost dominates purchasing for 74%. (Grafana 2025 Observability Survey) |
| **The AI-observability gap is wide open** | 92% of practitioners say they'd get real value from AI catching anomalies; only 57% do observability for their own AI systems in any capacity. (Grafana 2026 Observability Survey) |
| **GenAI semconv is unstable** | As of mid-July 2026 every `gen_ai.*` attribute, span, metric and event carries the "Development" stability badge. Not one is Stable. Teams instrumenting against 2025 guidance are emitting deprecated attributes today. ([dev.to/azena-ai](https://dev.to/azena-ai/opentelemetrys-genai-semantic-conventions-are-not-stable-yet-heres-what-actually-shipped-in-2026-3mke)) |
| **Sovereignty tailwind is real and structural** | EDPB named on-premise inference the strongest available LLM data protection mitigation. Gartner projects sovereign cloud IaaS at $80B in 2026, +35.6%; 75% of enterprises will have sovereignty strategies by 2030. Most "EU data centre" commitments deliver residency, not sovereignty. ([BeyondScale](https://beyondscale.tech/blog/ai-data-residency-sovereignty-gdpr-cloud-act)) |

## 2.3 Position matrix

| | Vendor MCP (Datadog, Grafana, Splunk, Dynatrace) | Open multi-backend gateways (`observability-mcp`, OpenObserve MCP) | Agentic SRE products (Grafana Investigations, Rootly, Traversal) | **Berserk today** | **Berserk after Epics B + C** |
|---|---|---|---|---|---|
| Data stays in your perimeter | ✗ (mostly remote-hosted) | ✓ | ✗ | ✓ | ✓ |
| Works with a 7B local model | ✗ | partial | ✗ | partial | ✓ |
| Model never authors query language | ✗ | ✓ | ✗ | ✓ | ✓ |
| Per-agent scoped access | vendor-side | enterprise tier only | vendor-side | ✗ | ✓ |
| Audited agent access, locally held | vendor-side | enterprise tier only | vendor-side | ✗ | ✓ |
| Reproducible / replayable answers | ✗ | ✗ | ✗ | partial | ✓ |
| Wrong-answer containment | ✗ | ✗ | ✗ | partial | ✓ |
| Backend coverage | 1 (own) | many | many | **1** | **1** |

**Read of the matrix:** after Epics B and C, Berserk is the only entry that is local,
small-model-native, reproducible *and* audited. That intersection is genuinely empty
today. The single remaining weakness — backend coverage — is also the one that
determines whether anyone outside the team can adopt it. Epic D is therefore not
optional; it is the gate on external usefulness.

## 2.4 OpenObserve — closest competitor, read in detail

Source: [openobserve.ai/mcp-server](https://openobserve.ai/mcp-server/) (MCP server is
labelled **Preview** as of 2026-08-16), plus their docs and EULA.

### What to adopt

1. **Just-in-time tool search.** Their line: don't load 147 tools upfront; agents use
   `tool_search` to find tools by keyword and fetch schemas just-in-time. Note the
   detail worth copying — schema is returned *with* the search result, so there is no
   second round trip before execution.
2. **Two-tiered policy enforcement.** They separate the gateway layer (which agents may
   reach the server) from native RBAC (which streams and queries those identities may
   execute). This is a cleaner decomposition than a single principal check and Epic C
   should adopt it: authentication and authorization stay separable so an enterprise
   MCP gateway can front Berserk without duplicating logic.
3. **Gateway compatibility as a documented deployment mode.** They name Cloudflare, Kong
   and ContextForge explicitly. Berserk already has trusted-proxy CIDR handling; make
   gateway-fronting a tested, documented mode rather than an accident.
4. **Audit as a marketed feature, not plumbing.** They sell "Immutable Tool Auditing."
   Epic C should ship with the same visibility.
5. **BYOAI framing.** "MCP decouples data from inference" — useful language, and an
   argument Berserk makes more strongly than they do.
6. **Lead-magnet analyzer.** They ship Datadog and Dynatrace bill analyzers. Our
   analogue is a *routing reliability* analyzer (see BM-19).

### Where we beat them

| | OpenObserve | Berserk |
|---|---|---|
| Query origin | NL → SQL/PromQL translation by the model | Fixed, named intent → byte-identical KQL |
| Reproducibility | none — same question may produce a different query | `kql_canonical_sha256` per execution |
| Stated hallucination defence | rate limiting, query timeouts, read-only execution — i.e. *the agent cannot destabilise the backend* | plus: empty-result disambiguation, window echo, validation rejection — i.e. *the agent cannot confidently report a false negative* |
| Translation latency | ~200–500ms inference per question (their own FAQ) | none on the fixed path |
| RBAC in the free/OSS path | **not supported** — "all users have unrestricted access to all features" | Epic C ships scoping in the open path |
| Audit trail in the free/OSS path | enterprise tier | Epic C ships it in the open path |
| Licence posture | revocable EULA; licensee may not interfere with telemetry/metering; carve-out requiring a separate addendum for PHI, cardholder data, or other heightened-safeguard personal data | no licence server, no metering, no vendor |
| Target model class | Claude / GPT-4 class | 7B local, measured |

**The sharpest wedge** is the OSS gating. Their own docs state RBAC is unsupported in the
open-source edition. This is already a reputational sore spot — an r/opensource thread
("So OpenObserve is 'open-source'… until you actually try using it") gained traction over
SSO and RBAC being enterprise-locked, and the founder acknowledged the README was
misleading. The self-hosting sovereignty buyer — our buyer — gets neither scoping nor an
audit trail without taking a revocable licence from a US vendor.

**The second wedge** is wrong-answer containment. Read their hallucination FAQ closely:
every control listed protects *system stability*. Nothing addresses an agent confidently
reporting a clean bill of health because its generated query silently matched zero rows.
That is the failure mode that actually pages someone at 4am, and no one in the category
is addressing it.

### Where they beat us, and it isn't close

- Maturity: 21k GitHub stars, SOC 2 Type II, ISO 27001, a company behind it.
- **Backend coverage.** They ingest OTel from anything. We work with bzrk. Every
  advantage above is a *quality* argument, and quality arguments lose to *availability*
  arguments.
- Correlation across logs, metrics and traces in one store.

### Consequence for our roadmap

The comparison a prospective user actually runs is: *"we already ingest OTel into
OpenObserve / Loki / Sentinel — can Berserk sit in front of it?"* That argues for making
the **first non-bzrk adapter OTel-shaped**, ahead of Sentinel. See Epic D and the open
question in §6.2.

---

# Part 3 — Strategy

## 3.1 Positioning

They own *"AI-native observability platform, self-hostable."*

The open lane is **"the answer layer you can prove."**

Four claims, in priority order:

1. Deterministic queries — we can prove what the agent asked, not just report what it found.
2. Wrong-answer containment as a first-class concern.
3. Per-agent scoping and a hash-chained audit trail, in the open path.
4. A tool surface small enough for a model running on hardware you own.

Do **not** position as "AI SRE" or "observability platform." The first claim is
market-fatigued; the second is a category we should explicitly cede.

## 3.2 What we deliberately do not build

- **Alerting, dashboards, ingestion pipelines.** That is where the eight-tools-per-org
  sprawl comes from and it means competing with Adaptive Telemetry on its own turf.
- **Autonomous remediation / write access.** Berserk's credibility is that it refuses
  rather than invents. Write access spends that in one bad incident.
- **More tools.** At 59 tools the routing surface is the binding constraint. Any
  proposal that grows the fixed tool count needs a corresponding removal.

## 3.3 Epic sequencing

```
Epic A  Foundations          ─┐  independent, ship first
                              │
Epic B  Routing surface      ─┤  BM-10 (principal refactor) is a prerequisite
                              │
Epic C  Principal + audit    ─┤
                              │
Epic D  Backend adapter      ─┘  gates external adoption
```

Hard ordering constraint: **BM-10 (principal refactor) lands before BM-6 (discovery) and
before BM-11 (ledger).** Discovery needs per-principal index filtering; the ledger needs
a principal to attribute to. Doing the refactor once avoids touching the visibility path
twice.

---

# Part 4 — Issue backlog

Format per issue: **Gap → Change → Acceptance → Risk.** Sized S (≤1 day),
M (2–4 days), L (1–2 weeks).

## Epic A — Foundations

Implementation-local quality work. No blueprint spec required except where noted.

---

### BM-1 — Result envelope for the fixed-query path
**Size:** M · **Blueprint spec required:** yes (changes output shape of 24 tools)
**Branch:** `feat/simple-result-envelope`

**Gap.** 24 tools dispatch through `SIMPLE` (`:1553-1577`) and return `bzrk` stdout
verbatim (`:2191-2194`):

```python
if name in SIMPLE:
    kql, default_since = SIMPLE[name]
    since = arguments.get("since") or default_since
    return bzrk_search(kql, since)
```

Two consequences:

- **The window is invisible.** Defaults differ per tool (15m `top_cpu`, 1h
  `errors_by_service`, 6h `soc_repeated_errors`) and are never echoed. A model that
  omits `since` gets rows with no idea what they span, then writes "there were 12
  errors" — a wrong sentence built on a correct query.
- **Empty results are a bare sentinel.** `bzrk_search` returns the literal `(no rows)`
  (`:1010`). For a small model that is ambiguous between *healthy*, *wrong window*,
  *wrong tool*, and *source stopped ingesting*.

`detect_anomalies` and `forecast_capacity` (`:2108-2145`) already do this correctly. The
pattern exists; it is not applied to the tools called most.

Related: the `stdout_overflow` message (`:999-1004`) tells the model to project fewer
columns or add a take bound. On a `SIMPLE` tool the query is fixed — the only lever is
`since`. Fix that message on the fixed-query path.

**Change.**
- `_envelope(tool, since, out)` applied on the `SIMPLE` path only.
- Header: resolved window + row count. **Raw rows verbatim underneath** so nothing
  downstream that greps output breaks.
- Static per-tool next-step table for the empty case. No LLM, no new queries.
  e.g. `errors_by_service` empty → "No ERROR rows in <window>. Widen with
  `since='24h ago'`, or confirm the source is reporting with `list_services`."
- Gate behind `BERSERK_MCP_ENVELOPE=0` for one release.
- Rewrite the overflow message on the fixed-query path to name `since` as the only lever.

**Acceptance.**
- Unit tests with stubbed `bzrk` (pattern established in `tests/`), one per case: rows
  present, `(no rows)`, overflow, auth failure.
- Router/answer cases where the pass condition is "model does not assert a false
  negative" given `(no rows)`.
- `tests/test_berserk_mcp.py` and `evals/mcp_protocol_smoke.py` pass unchanged with the
  env gate off.

**Risk.** Output-shape change across 24 tools. The env gate and verbatim-rows-underneath
rule are the mitigations. Version bump + changelog note required.

---

### BM-2 — Constrain and normalize `since`
**Size:** S · **Blueprint spec required:** no · **Ship first**
**Branch:** `feat/since-schema-and-normalizer`

**Gap.** `since` appears on 51 of 59 tools. Its schema (`:1472-1473`) is:

```python
{"since": {"type": "string",
           "description": "Time window e.g. '15m ago', '1h ago', '2d ago'."}}
```

No `pattern`, no `enum`, no `examples`, no `default`. The only `pattern` in the whole
tool surface is on `claude_record_recommendation_decision`. Validation is `_SINCE_RE`
(`:1042`), accepting `now` or `<int><unit>[ ago]`; a miss costs a full round trip
(`:1079-1083`).

This matters twice:

- **Grammar-constrained decoding is the main lever for small-model reliability** —
  structured outputs, GBNF, JSON-schema-constrained sampling. All need something in
  `inputSchema` to constrain against. Today there is nothing, so `since` is the last
  field on the fixed-query path where the model may guess syntax. That is the exact
  failure mode the project exists to eliminate, surviving on 86% of tools.
- **The forms small models emit all fail**: `last 24 hours`, `yesterday`, `past week`,
  `2026-08-15`. (`24h` passes; `last 24 hours` does not.)

**Change.**
1. Add `pattern`, `examples` and an explicit per-tool `default` to the `since` schema.
   The `default` also makes the currently invisible per-tool window visible.
2. Add `_normalize_since()` mapping common natural forms onto the canonical grammar
   **before** validation.
3. Keep `_SINCE_RE` as the post-normalization gate. **The normalizer emits only canonical
   forms** — nothing new reaches `bzrk`, validation posture unchanged.

**Acceptance.**
- Table-driven test over ~30 model-realistic strings: normalized, unchanged, rejected.
- **Invariant assertion:** for every input the normalizer accepts, its output satisfies
  `_SINCE_RE`. This is the security-relevant property.
- Add `expect_since_any` assertions to more `evals/router_cases.jsonl` cases (the field
  exists but only `errors_24h` uses it).
- Report retry-rate delta from `run_eval.py` before/after.

**Risk.** Low. Additive and pre-validation.

---

### BM-3 — `--doctor` preflight and `self_check` tool
**Size:** M · **Blueprint spec required:** no
**Branch:** `feat/doctor-preflight`

**Gap.** 42 env vars read; 11 documented in `.env.example` (HTTP transport + the 2026
protocol gate). `BZRK_BIN`, `BZRK_PROFILE`, `BZRK_TIMEOUT`, `BERSERK_TABLE`,
`BERSERK_MCP_ROLE`, `BERSERK_MCP_REDACT*`, `BERSERK_MCP_LEARNED_PATH`,
`BERSERK_MCP_PSEUDONYM_KEY`, the LLM-ladder vars and the CanonLoom pair are documented
only inside a 1,669-line README.

`main()` (`:3520`) has 16 CLI flags and no validation entry point. The server does exactly
one fail-fast check at import: unknown `BERSERK_MCP_ROLE` (`:466-471`, F-008). Everything
else fails late and opaquely, addressed to an agent rather than an operator. The agent
cannot distinguish "wired wrong" from "genuinely nothing to report."

The README's positioning is air-gapped, sovereign, fleet deployment. Those are precisely
the environments where you cannot iterate interactively. F-008 already states the right
philosophy; this extends it from one variable to the whole config surface.

**Change.**
- `berserk-mcp --doctor`: ordered checks, pass/fail/skip table, one exact remediation
  line per failure.
  1. `bzrk` resolvable via `BZRK_BIN` or PATH
  2. `bzrk` version
  3. auth under configured `BZRK_PROFILE`
  4. `BERSERK_TABLE` reachable
  5. row count in the last 1h
  6. primers dir resolvable for active role
  7. learned-store path writable
  8. HTTP config coherent when `BERSERK_MCP_HTTP_ENABLE=1`
  9. LLM ladder / CanonLoom reachability — **skipped when unconfigured, not failed**
- Exit `0` pass / `1` degraded / `2` broken. Works as a fleet readiness probe and
  container healthcheck.
- Same checks behind a `self_check` tool (author/deep tier per BM-8).
- Extend `.env.example` to all 42, grouped, with the default the code actually applies.

**Acceptance.**
- Stubbed-`bzrk` unit tests per failure mode, independently triggerable.
- CI runs `--doctor` against the stub and asserts each exit code.
- **Drift test:** grep the codebase for env reads; fail the build if `.env.example` does
  not cover them.
- Assert no secret value is ever printed — route echoed values through `secret_scan.py`.

**Risk.** `--doctor` touches the live backend; must respect existing budget/timeout guards.

---

### BM-4 — Project saved and generated query packs into `tools/list`
**Size:** M · **Blueprint spec required:** yes
**Branch:** `feat/project-saved-queries-as-tools`

**Gap.** The README comparison table claims custom-query persistence yields "named,
reusable tools." It does not. Saved queries are rows in a JSON store reachable only
through a two-step prose indirection (`:1859-1893`):

```
list_saved  →  "- disk_pressure_by_host: which hosts are near disk saturation"
            →  run_saved(name="disk_pressure_by_host")
```

They never appear in `tools/list`. `_BASE_INSTRUCTIONS` (`:386-398`) tells the model how
to *create* a saved query but never mentions any exist, and never names `list_saved` — so
on a cold session the model must speculatively probe a store it has no reason to believe
is populated.

This is where the most valuable machinery in the repo deposits its output.
`generate_parser` and `run_discovery_worker` write into the same store with
`origin: "generated"` (`:1364`). Auto-authored, execute-verified query packs for newly
onboarded sources are the *least* discoverable part of the surface — backwards for a
fixed-query design.

**Change.**
- Project visible learned items into `tools/list` as `saved__<name>`: description from
  the stored description, `since` default from the stored value, `roles` honoured via
  existing `item_visible`.
- Dispatch maps onto the current `run_saved` path. Validation, schema-drift check and
  budget logic do not move.
- Flip `capabilities.tools.listChanged` — hardcoded `false` at `:2717` and `:3116` — and
  emit `notifications/tools/list_changed` after a successful `save_query` or worker write.
- **Cap the projection** (N most recent, role-scoped). Keep generated descriptions inside
  their `<generated-description>` fencing so the v1.23 sanitization posture holds.

**Acceptance.**
- `evals/mcp_protocol_smoke.py` asserts a saved query appears in `tools/list` and is
  directly callable.
- Assert `notifications/tools/list_changed` fires after `save_query` and does **not** fire
  on a rejected save.
- Router cases where the correct answer is a projected saved tool rather than `search`.

**Risk.** Surface growth is what BM-8 fights. Cap and role scoping are load-bearing.
Note: BM-6 (discovery) substantially reduces this risk — consider sequencing BM-4 after
BM-6 so projected tools land into a discovery surface rather than a flat list.

---

### BM-5 — Wrong-answer containment: name it, test it, document it
**Size:** S · **Blueprint spec required:** no
**Branch:** `feat/wrong-answer-containment`

**Gap.** Competitors' hallucination story is exclusively operational (rate limits,
timeouts, read-only). Berserk already has real *answer*-level defences —
`_BASE_INSTRUCTIONS` warns that a bare column name silently matches zero rows,
`_validate_user_kql` rejects blockers, schema-drift warns on saved queries — but they are
scattered and unnamed.

**Change.**
- Consolidate into a documented, tested set of invariants under one name.
- Add an eval class: for each containment control, a case that would produce a confident
  false negative without it, asserting the model does not.
- README section: "Wrong-answer containment" listing each control, what it prevents, and
  the test that proves it.

**Acceptance.** Every listed control has a named failing-without-it test. No new runtime
behaviour required — this issue may be pure test + docs if the controls already hold.

**Risk.** None. Highest marketing-value-per-line-of-code item in the backlog.

---

## Epic B — Routing surface

---

### BM-6 — `find_tool`: just-in-time tool discovery
**Size:** L · **Blueprint spec required:** yes · **Depends on:** BM-10
**Branch:** `feat/jit-tool-discovery`

**Gap.** ~3,500 tokens of tool definitions per lane is the single largest tax on a 7B
context window, and it grows every time the parser factory learns something. Industry
data: 200–500 tokens per tool definition; Copilot's 40→13 reduction produced measurable
latency *and* accuracy gains simultaneously.

**Change.**
- New tool `find_tool(intent: str) → candidates[]`, each candidate carrying **full
  `inputSchema` inline** so there is no second round trip before execution.
- Keyword index built at startup from `TOOLS` + the learned store. In-memory dict,
  stdlib only.
- **Index must be principal-filtered** (see BM-10) — discovery must not reveal the
  existence of tools the caller cannot invoke.
- Anchor set: ~8 tools that stay resident. **Define by a stated rule, not a hand-picked
  list**, or it will drift.
- `BERSERK_MCP_DISCOVERY=1|0` kill switch for one release.
- Low-confidence fallback: return the anchor set and say so explicitly.

**Acceptance.**
- **Recall gate in CI:** for each tool, N phrasings; assert the tool appears in the top-5
  candidates. This is a hard gate — with discovery, a tool that does not surface is
  *unreachable*, because the model cannot browse.
- Two-hop eval scoring: `find_tool` hop and tool hop scored separately, so a regression is
  attributable.
- Measured `tools/list` token size before/after, emitted as a CI artifact.
- Client compatibility matrix: Claude Code, Codex, Hermes runtime tested independently —
  `tools/list` caching behaviour differs and is where this will bite. `_discover_result()`
  already sets `ttlMs` and `cacheScope: private`; verify it is honoured.

**Risk.** **Risk is concentrated in retrieval quality, not code.** Budget more time for
the index than the plumbing. Today a bad description costs a recoverable mis-route; with
discovery it costs reachability.

---

### BM-7 — Rewrite instructions and primers for the discovery loop
**Size:** S · **Depends on:** BM-6
**Branch:** `feat/discovery-instructions`

**Gap.** `_BASE_INSTRUCTIONS` (`:386`) explicitly names `top_cpu`, `errors_by_service`,
`logs_for_service`, `host_cpu` and explains the per-host vs per-container distinction.
If those tools are not resident, that text is **actively misleading**. Same for the five
`_ROLE_PREFIX` strings and `primers/*.md`.

**Change.** Rewrite to teach the discovery loop. Move per-tool disambiguation guidance
(per-host vs per-container, `top_cpu` vs `host_cpu`) out of the global instructions and
into the tool descriptions and the keyword index, where discovery can surface it at the
point of need.

**Acceptance.** No tool name appears in `_BASE_INSTRUCTIONS` unless it is in the anchor
set. Add a test asserting this — it will catch drift as the anchor set changes.

**Risk.** Low, but it is the item most likely to be forgotten. Ship in the same release
as BM-6.

---

### BM-8 — Tool tiers (small / author-deep)
**Size:** M · **Blueprint spec required:** yes · **Depends on:** BM-10
**Branch:** `feat/tool-tiers`

**Gap.** Role lanes filter by job function only, with no notion of tier. `search` —
free-text KQL — is **untagged, so it ships in every lane** (`:1629`), with a description
that invites use ("Use when the other tools do not fit"). So do `validate_kql` (tagged
with all four roles = effectively ALL), `suggest_ingestion`, and all five `canonloom_*`
tools. An on-call SRE lane carries the CanonLoom pipeline as routing noise, and a 7B model
that cannot find a fit will reach for `search` and author bad KQL — reproducing the exact
problem the README opens by describing.

**Change.**
- `tier` dimension orthogonal to lane.
  - **small:** fixed-intent tools only.
  - **author/deep:** `search`, `validate_kql`, `save_query`, `generate_parser`,
    `review_generated`, `canonloom_*`, `suggest_ingestion`, `self_check`.
- Default small-tier-on when the principal resolves to a single operational lane. Leave
  `all` untouched for backwards compatibility.
- **Mirror the F-008 fail-fast pattern** (`:466-471`) and log the applied tier at startup.
  Silently hiding a tool an operator relies on is the main risk; make it loud.

**Acceptance.**
- Extend `evals/router_cases.jsonl` with lane labels and confusable-pair cases. The
  `top_cpu` vs `host_cpu` and `top_memory` vs `host_memory` disambiguation is already
  written into descriptions as prose — promote it to cases.
- Wire `python evals/run_eval.py --backend mock evals/router_cases.jsonl` into
  `.github/workflows/ci.yml`. CI today runs unittest + protocol smoke and never invokes
  the eval harness.
- Emit per-lane tool count and `tools/list` byte size as a CI artifact.

**Risk.** Visibility changes can hide a needed tool. Startup logging + fail-fast mitigate.

**Note.** BM-6 substantially supersedes this — tiering trims the surface once, discovery
makes surface size stop mattering. Keep BM-8 because gating the KQL escape hatch is
independently valuable, but it is **only defensible once BM-4 has landed**: hiding the
fallback requires supplying the replacement.

---

### BM-9 — Router eval expansion and CI gate
**Size:** M
**Branch:** `feat/router-eval-expansion`

**Gap.** 31 cases covering 21 of 59 tools, no lane dimension, never run in CI.

**Change.**
- Expand to cover every tool in the small tier, minimum 3 phrasings each.
- Add lane labels and confusable pairs.
- Add `expect_since_any` broadly (BM-2).
- Two-hop scoring (BM-6).
- CI gate with a published threshold. Start at the current measured pass rate, ratchet up.

**Acceptance.** CI fails on regression. Per-tool recall reported. Baseline published in
`evals/results/`.

**Risk.** None. This is the measurement infrastructure everything else is judged against —
consider pulling it forward ahead of BM-6.

---

## Epic C — Principal and audit

---

### BM-10 — Principal refactor: role becomes per-request
**Size:** L · **Blueprint spec required:** yes · **Blocks:** BM-6, BM-8, BM-11
**Branch:** `refactor/per-request-principal`

**Gap.** `ACTIVE_ROLE` is a process-global resolved at import time, and `INSTRUCTIONS` is
computed once from it at import (`:466-473`).

**This is the structural blocker for the whole epic** and ~80% of its effort. Ship it as
its own commit with **no behavioural change** before anything else in Epic C.

**Change.**
- Introduce a `Principal` object: `{agent_id, roles[], tier, allowed_tables[],
  max_window, read_only}`.
- `tool_visible()`, `item_visible()`, `build_instructions()`, `normalize_roles()` and
  every read of `ACTIVE_ROLE` take a principal argument.
- `INSTRUCTIONS` computed per session rather than at import.
- Default principal derived from env preserves today's behaviour exactly.

**Acceptance.**
- **Zero behavioural diff.** The entire existing test suite and protocol smoke pass
  unmodified. If any test needs changing, the refactor has changed behaviour and should
  be re-scoped.
- No remaining module-level reads of `ACTIVE_ROLE` outside the default-principal
  construction — assert with a grep test.

**Risk.** Highest-risk item in the backlog. It touches the visibility path, which is
security-relevant. Mitigation: no-behaviour-change constraint plus the grep test.

---

### BM-11 — Hash-chained audit ledger
**Size:** M · **Blueprint spec required:** yes · **Depends on:** BM-10
**Branch:** `feat/audit-ledger`

**Change.**
- New `audit.py`: append-only JSONL, fsync, rotation, **per-record hash chaining to the
  previous record**. The chain is what makes it evidence rather than a log file — cheap
  now, near-impossible to retrofit credibly.
- Record schema: `ts_utc`, `principal_id`, `transport`, `tool`, `kql_canonical_sha256`,
  `resolved_since`, `row_count`, `bytes_out`, `redaction_rules_applied`, `latency_ms`,
  `outcome`, `prev_hash`, `record_hash`.
- Emit point: `bzrk_search`. Single chokepoint — every query path is covered by one
  insertion.
- `find_tool` calls get their own record type including the candidate list returned.
- `_store.py` gains an append-only path with its own locking; the existing
  load-whole-list/save-whole-list pattern does not apply.

**Two invariants, into the spec before any code:**

1. **The ledger never contains telemetry rows or query results.** Hashes, counts and
   metadata only. Violating this creates a second copy of production data with weaker
   controls than the primary — the exact thing the product exists to avoid.
2. **stdio has no authenticated principal.** Scoped credentials are an HTTP-transport
   feature. stdio gets a single locally-configured principal from env, and the ledger
   records `transport: stdio` so an auditor sees the distinction. Pretending otherwise is
   worse than the limitation.

**Acceptance.**
- Chain verification test: tamper with a record, assert detection.
- Assert redaction records rule IDs, never matched values.
- Assert no row content in any record, property-tested against varied `bzrk` stub output.
- Retention configurable; no silent default. Ledger retention and telemetry retention are
  different obligations.

**Risk.** Invariant 1 is the one to guard in review.

---

### BM-12 — Scoped principals for the HTTP transport
**Size:** M · **Depends on:** BM-10
**Branch:** `feat/scoped-principals`

**Change.**
- Principal store: `token_hash → Principal`.
- `BERSERK_MCP_HTTP_AUTH_TOKEN` goes from shared secret to token→principal lookup.
  **Existing single-token config keeps working** as one principal with today's global role.
- `--mint-token`: issue a scoped credential, print once, store the hash.
- Enforce `allowed_tables`, `max_window`, `read_only`, tier and lane per request.
- **Keep authentication and authorization separable** so an enterprise MCP gateway
  (Cloudflare / Kong / ContextForge) can front Berserk without duplicating logic.

**Acceptance.**
- Backwards-compat test: existing single-token config, unchanged, behaves identically.
- Per-scope denial tests: out-of-scope table, over-long window, write attempt.
- Denials are ledger-recorded.
- Gateway-fronted deployment documented and smoke-tested.

**Risk.** Auth path. Requires careful review; see the Codex rubric §5.3.

---

### BM-13 — Audit export and verification CLI
**Size:** S · **Depends on:** BM-11
**Branch:** `feat/audit-export`

**Change.** `--audit-export` (time-bounded evidence pack with chain verification, for
NIS2/DORA/ISO) and `--audit-verify` (independent chain integrity check).

**Acceptance.** Export is self-contained and verifiable without the running server.
Verification detects truncation, reordering and mutation.

---

### BM-14 — Ledger as eval corpus
**Size:** S · **Depends on:** BM-11, BM-6
**Branch:** `feat/ledger-eval-corpus`

**Gap.** We have 31 synthetic router cases and a ≥95% target with no production
measurement.

**Change.** A tool that converts ledger records into router eval cases. Real two-hop
routing data, locally, containing no telemetry content. A model calling a tool *outside*
the candidate list `find_tool` returned is a directly detectable routing failure.

**Acceptance.** Generated cases pass schema validation and are usable by `run_eval.py`
unmodified.

**Note.** This is the strongest synergy between Epics B and C, and the path from synthetic
cases to a real measurement of the ≥95% target. Design the BM-11 record schema with this
explicitly in mind.

---

## Epic D — Backend adapter

---

### BM-15 — Backend abstraction layer
**Size:** L · **Blueprint spec required:** yes
**Branch:** `feat/backend-abstraction`

**Gap.** Berserk works with bzrk. Every quality advantage in §2.3 is moot if the answer to
"does it work with our stack" is no. This is the gate on external adoption.

**Change.** Extract a backend interface behind the existing fixed-query surface: execute,
schema discovery, canonicalization, budget/timeout. bzrk becomes the first implementation
with no behavioural change.

**Acceptance.** Zero behavioural diff for bzrk. Full test suite passes unmodified.

---

### BM-16 — Second backend adapter
**Size:** L · **Depends on:** BM-15 · **Target: open — see §6.2**
**Branch:** `feat/adapter-<target>`

Candidates, in the order the market argues for:

1. **OTel-shaped store** (Loki / OpenObserve / generic OTLP-backed). Answers the question
   a prospective user actually asks: "we already ingest OTel — can Berserk sit in front
   of it?" Largest reach.
2. **Defender XDR / Sentinel.** Same query language, so the fixed-query machinery
   transfers nearly intact; makes the `soc` lane a real product; highest internal value.

**Acceptance.** The same tool names answer correctly against both backends. Router evals
pass against the new adapter. Canonical hashing works per-backend.

---

### BM-17 — Cross-backend canonicalization and provenance
**Size:** M · **Depends on:** BM-16
**Branch:** `feat/cross-backend-provenance`

**Gap.** `kql_canonical_sha256` is KQL-specific. The reproducibility claim — our primary
differentiator — must survive a second query language.

**Change.** Provenance records carry backend identity plus a backend-specific canonical
hash, with a stable intent identifier above both. An answer is reproducible *within* a
backend and *attributable* across backends.

**Acceptance.** Provenance round-trips across both adapters. Replay test proves an
investigation is re-executable per backend.

---

## Epic E — Positioning

---

### BM-18 — README restructure
**Size:** M

1,669 lines / 109 KB in a single file is a real adoption cost for a project asking others
to audit it in an afternoon. Split into README (thesis + quickstart + the four claims from
§3.1), `docs/deployment.md`, `docs/security.md`, `docs/tools.md` (generated from `TOOLS`),
`docs/compliance.md` (Epic C output).

---

### BM-19 — Routing reliability analyzer
**Size:** S

Competitors ship bill analyzers as lead magnets. Ours: point a script at any MCP server's
`tools/list`, get back tool-definition token cost, tool count, and an estimated
small-model mis-route rate. Runs against Berserk *and* competitors. Directly dramatizes
the claim in §3.1 item 4.

---

# Part 5 — Review rubric for Codex

Apply to every PR. Findings are **Blocking** or **Advisory**.

## 5.1 Universal — every PR

| # | Check | Severity |
|---|---|---|
| U1 | No third-party imports added. stdlib only. | Blocking |
| U2 | Diff is minimal — no drive-by reformatting, no unrelated refactors. | Blocking |
| U3 | All timestamps UTC. | Blocking |
| U4 | Line references in the PR description resolve at HEAD. | Advisory |
| U5 | Exact changed-file summary present in the PR description. | Blocking |
| U6 | Existing tests pass unmodified, or every modification is justified in the description. | Blocking |
| U7 | No secret value can reach stdout, stderr, a log line, or the ledger. | Blocking |
| U8 | New env vars added to `.env.example` with the default the code applies. | Blocking |
| U9 | Public behaviour change → version bump + changelog entry. | Blocking |
| U10 | For PRs marked "blueprint spec required": link to the merged `berserk-blueprint` spec. | Blocking |

## 5.2 Fixed-query invariants — any PR touching the query path

| # | Check | Severity |
|---|---|---|
| Q1 | The model is never given a path to author raw query text outside the author/deep tier. | Blocking |
| Q2 | `_SINCE_RE` remains the final gate. Any normalizer emits only canonical forms. | Blocking |
| Q3 | Table prefix validation unchanged. | Blocking |
| Q4 | Budget and timeout guards apply to every new execution path. | Blocking |
| Q5 | Empty results are never returned as a bare sentinel to the model. | Blocking |
| Q6 | Any new tool description states its disambiguation from its nearest confusable sibling. | Advisory |

## 5.3 Security-sensitive — BM-10, BM-11, BM-12, BM-13

| # | Check | Severity |
|---|---|---|
| S1 | **Ledger contains no telemetry rows or query results.** Hashes, counts, metadata only. | Blocking |
| S2 | Redaction records rule IDs, never matched values. | Blocking |
| S3 | Scope enforcement happens server-side, never trusting a client-supplied role, tier or table. | Blocking |
| S4 | Denials are ledger-recorded, not silently dropped. | Blocking |
| S5 | Token comparison is constant-time; tokens stored hashed, never plaintext. | Blocking |
| S6 | `transport: stdio` records are distinguishable from authenticated HTTP records. | Blocking |
| S7 | Hash chain covers the full record including `prev_hash`. | Blocking |
| S8 | Existing single-token config behaves identically after BM-12. | Blocking |
| S9 | Failing open is never the error path for an authorization check. | Blocking |

## 5.4 Routing surface — BM-6, BM-7, BM-8, BM-9

| # | Check | Severity |
|---|---|---|
| R1 | Discovery index is principal-filtered — no tool the caller cannot invoke is discoverable. | Blocking |
| R2 | Recall gate present and passing for every affected tool. | Blocking |
| R3 | No tool name in `_BASE_INSTRUCTIONS` or `_ROLE_PREFIX` unless it is in the anchor set. | Blocking |
| R4 | Anchor set defined by a stated rule, not a hand-picked list. | Advisory |
| R5 | Per-lane tool count and `tools/list` byte size reported in the CI artifact. | Advisory |
| R6 | Client compatibility matrix updated (Claude Code, Codex, Hermes). | Blocking |

## 5.5 Review posture

- **Reject scope creep.** A PR that fixes an unrelated bug it noticed should file an issue
  instead. The backlog depends on attributable regressions.
- **Reject added tools.** Any PR growing the fixed tool count without a corresponding
  removal needs explicit sign-off against §3.2.
- **Reject "it's tested by the eval."** Evals are stochastic. Deterministic behaviour needs
  a deterministic test.

---

# Part 6 — Open questions for the team

## 6.1 Anchor set composition (BM-6)

What is the *rule*? Proposal: the N tools that account for ≥80% of historical calls, plus
`find_tool`, `list_services` and `self_check`. That requires call-frequency data we do not
have until BM-11 ships — which argues for shipping the ledger before finalising the anchor
set, and running BM-6 initially with a hand-picked set flagged as provisional.

## 6.2 First non-bzrk adapter target (BM-16)

**OTel-shaped vs Sentinel.** Earlier analysis favoured Sentinel (query-language reuse,
internal value). The OpenObserve read-through changes the balance: the comparison a
prospective user actually runs is "can Berserk sit in front of the OTel store we already
have," and OTel-shaped is the larger reach. Sentinel is the higher-conviction internal
bet. **Decide before BM-15 lands** — the abstraction shape follows from the answer.

## 6.3 Ledger retention default

Different obligation from telemetry retention. Needs a stated policy, not an implicit
default. Prior Compliance API work in this org is the relevant precedent.

## 6.4 Do we ship a headless CLI lane?

Datadog (Pup) and Grafana (`gcx`) both shipped one alongside their MCP server, which
suggests a real demand signal for scripting and high-scale agent workflows. Berserk's
`main()` already has 16 flags — a coherent CLI lane may be closer than it looks. Not in
this backlog; flagging for a future planning cycle.

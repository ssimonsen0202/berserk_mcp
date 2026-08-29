# berserk-mcp

[![CI](https://github.com/ssimonsen0202/berserk_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ssimonsen0202/berserk_mcp/actions/workflows/ci.yml)

berserk-mcp is an [MCP](https://modelcontextprotocol.io) server. It lets an
LLM answer [Berserk](https://bzrk.dev) observability questions. The LLM
**calls tools** for this. The LLM does not write KQL by hand.

> **Why this matters:** A raw query language makes a model guess. It guesses
> wrong table names, wrong field names, and broken aggregations. Each wrong
> guess costs you a retry. Every tool in berserk-mcp wraps one *verified*
> Kusto/KQL query. The model picks an intent — for example `top_cpu`,
> `errors_by_service`, or `sre_host_headroom`. The query itself stays fixed.
> This fixed-query design is the core idea. It lets even small or cheap
> models answer observability questions reliably.

- **Works with Claude Desktop, Claude Code, and any MCP client.** By default, berserk-mcp speaks MCP protocol version `2025-06-18` over stdio (newline-delimited JSON-RPC 2.0). It implements every required method — `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call` — with strict envelope validation and adversarial regression tests. See [Connect it to a client](#connect-it-to-a-client) for `claude_desktop_config.json` and `claude mcp add` recipes.
- **MCP compatibility baseline.** The stable default remains `2025-06-18` stdio. Additive `2026-07-28` MCP features are available only when explicitly enabled with `BERSERK_MCP_ENABLE_2026_07_28=1`, so existing clients keep their legacy response shapes. See [MCP 2026-07-28 adaptation baseline](docs/mcp-2026-07-28-adaptation.md).
- **Optional HTTP transport is closed by default.** stdio remains the default. HTTP opens no listener unless explicitly enabled, defaults to loopback, and fails closed for remote bind unless auth, Host allowlisting, and CIDR allowlisting are configured. See [MCP HTTP transport and reverse proxy deployment](docs/mcp-http-reverse-proxy.md) and [.env.example](.env.example).
- **Zero dependencies.** berserk-mcp uses only the Python standard library. You do not `pip install` anything beyond the package itself. (The optional LLM parser factory uses `urllib`. It still adds no third-party dependency.)
- **Small and auditable.** berserk-mcp is standard-library-only. Its focused modules cover the MCP server, parser generation, Claude analytics, AI FinOps, KQL validation, schema snapshots, secret redaction, and ingestion advice. You can read, audit, and vendor each module without pulling in a framework.
- **Cross-platform.** berserk-mcp runs anywhere the `bzrk` CLI runs, including Windows.
- **Safe by construction.** berserk-mcp uses fixed queries. It validates input on every free-text tool. It never calls `shell=True`. The Berserk token never touches this code.
- **Self-extending (new in 1.7).** An optional [parser factory](#parser-factory-llm-generated-query-packs) detects *new* sources arriving in Berserk. It uses an LLM to author, execute-verify, and save KQL "query packs" for each new source. The design follows Microsoft Sentinel's [ASIM parser AI agent](https://learn.microsoft.com/en-gb/azure/sentinel/normalization-create-parsers-ai-agent). It tries cheap providers first, enforces hard runaway fail-safes, and never lets a generated query overwrite a human one.
- **Knowledge-artifact lifecycle bridge.** An optional [CanonLoom bridge](#canonloom-knowledge-artifact-lifecycle-bridge) exposes a separate, self-hosted service that turns a source URL into a validated, versioned skill artifact. This covers source acquisition, relevance scoring, artifact-diff comparison, generation, structural/injection validation, and git-committed promotion. berserk-mcp only speaks to CanonLoom's HTTP API. None of CanonLoom's own dependencies (FastAPI, Anthropic, `pygit2`, and its stricter Python 3.14+ floor) touch berserk-mcp's own zero-dependency footprint.

> ## ⚠️ Disclaimer — please read
>
> berserk-mcp is an **unofficial, community-built** project. The Berserk project
> and its maintainers do **not** sponsor, endorse, support, or affiliate with it.
> berserk-mcp talks to Berserk only through the public `bzrk` CLI. It uses no
> internal API and no reverse engineering.
>
> berserk-mcp is provided **as-is, with no warranty and no liability**. This
> covers any use, outcome, downtime, data loss, or cost (see [LICENSE](LICENSE)).
> You run berserk-mcp at your own risk against your own infrastructure. Pointing
> it at a production Berserk is your decision.
>
> For bugs, feature requests, and questions about *this server*: open an issue
> in this repository. For questions about Berserk itself: contact the Berserk
> project, not this repository.

## Release history

Current version: **1.27.0**. This is a bullet-point overview, most recent
first — full detail for each notable release lives in
[`docs/releases/`](docs/releases/).

- **v1.27.0** (2026-08-28) — Five security fixes from an independent Codex
  review: KQL validator bypasses, unfenced telemetry attributes, a gap in
  the injection delimiter, an OAuth-header redirect leak, and a bug that
  read the text `"false"` as true. Also adds `investigate_error_rate`
  (issue #24): a fixed, step-by-step tool that walks a decision tree to
  find the cause of an elevated error rate, scoped to the SRE/Ops lane.
  See [details](docs/releases/v1.27.0.md).
- **v1.26.0** (2026-08-24) — Untrusted-data fencing, tool tiers, just-in-time
  tool discovery (`find_tool`, 92% measured token reduction), a live
  quota-window check (`claude_quota_status`), an `agent` parameter on the base
  Claude Code query tools for querying other ingested agents' data, and a
  schema-fetcher bug fix (a failed backend call was silently cached as a
  "fresh" schema). See [details](docs/releases/v1.26.0.md).
- **v1.25.1** (2026-08-11) — Performance bugfixes for shipped KQL: selective
  service filtering, shallower unfiltered schema discovery, CI cost guardrails,
  and validator-derived query-budget headroom. See
  [details](docs/releases/v1.25.1.md).
- **CanonLoom bridge** (2026-08-03, commit `033d855`, "CLP-9 CanonLoom MCP
  tool bridge") — five new tools (`canonloom_run_pipeline`,
  `canonloom_list_artifacts`, `canonloom_get_artifact`,
  `canonloom_freshness_report`, `canonloom_run_history`) bridging to a
  separately-run `canonloom-server`. Landed after v1.24.0 with no dedicated
  release-notes entry; see
  [CanonLoom bridge](#canonloom-knowledge-artifact-lifecycle-bridge) below.
- **v1.24.0** (2026-07-31) — MCP 2026-07-28 adaptation and safe-default HTTP
  transport: gated modern discovery, modern result envelopes, structured
  reporting output, private cache hints, input-required guidance, in-memory
  task lifecycle support, and a closed-by-default HTTP listener with auth,
  Host, CIDR, request-size, concurrency, and reverse-proxy guidance.
  See [details](docs/releases/v1.24.0.md).
- **v1.23.0** (2026-07-27) — Security remediation Phase 3: generated-content
  sanitization, per-deployment HMAC owner pseudonyms, spreadsheet-safe CSV,
  model-facing fence hardening, mandatory Discord egress redaction, scrubbed
  public deployment examples, and honest fixed-window Grafana dashboards.
  See [details](docs/releases/v1.23.0.md).
- **v1.22.0** (2026-07-27) — Security remediation Phases 0–2: execution-boundary
  KQL guards, bounded `bzrk` output, trusted binary resolution, deterministic
  FinOps redaction, shared cross-platform private stores, hardened HTTP for
  every outbound caller, strict primer configuration, and offline regression
  coverage. See [details](docs/releases/v1.22.0.md).

Older releases (v1.2.0 through v1.21.1 — enterprise Claude AI FinOps,
schema-grounded KQL validation, distributed-trace tools, agent-log
analytics, fleet-friendly worker tuning, the parser factory's early
fail-safes, role profiles, and the initial release) are in
[`docs/releases/`](docs/releases/), one file per version.

## Why this exists

**Berserk** is a self-hosted, OTEL-native, schemaless observability engine. It
handles petabyte scale. Berserk ingests logs, metrics, and traces over OTLP.
You query this data with a Kusto-style language (KQL), through the `bzrk`
CLI or the web UI. Berserk is [headless by design](https://www.bzrk.dev). It
is built for agents that ask questions, not for dashboards. Berserk supplies
the storage and the query engine. On its own, Berserk still assumes the
asker — human or agent — already knows KQL.

**The gap.** LLMs handle raw query languages badly. If you point a model at
`bzrk` directly, it invents table names, mistypes fields, and wastes tokens
on retries. Two fixes seemed obvious at first: paste the schema into the
prompt, or give the model few-shot KQL examples. Neither fix held — the
model kept guessing. Hardcoding the queries did work.

**What berserk-mcp adds.** berserk-mcp is a translation layer in front of
Berserk. It exposes observability *intents* as MCP tools — for example
`top_cpu`, `errors_by_service`, `sre_service_health`. Each tool wraps a
query that berserk-mcp has already verified against the live schema. The
model never authors KQL. It only picks an intent and a time window.
berserk-mcp does **not** replace Berserk's storage, query engine, or UI. It
makes them **agent-accessible and reliable on small, cheap, or local
models**.

Beyond the fixed tools, berserk-mcp adds three layers that default Berserk
does not have:

1. **Role lanes** — tool visibility filtered by job function, so each agent sees only the tools it needs
2. **Discovery queue and auto-KQL worker** — automated onboarding for new telemetry sources
3. **Amendments log** — every `save_query` write is tracked, so a worker can post changelogs and keep the query store auditable

| Approach | Result |
|---|---|
| Berserk web UI / `bzrk` CLI | Good for a human who knows KQL. Not usable by an agent. |
| Point an LLM at the raw CLI and schema docs | Unreliable. Models guess table and field names, and pay for retries. |
| A generic "text-to-KQL" MCP | Still *authors* queries. Same guessing problem, one layer up. |
| **berserk-mcp** | Fixed, verified queries. The model only picks a tool and a time window — no KQL authoring. See [Choosing a model](#choosing-a-model) for the measured reliability floor by model size. |

### What this adds vs. default Berserk

Berserk is a strong human-facing observability backend on its own. berserk-mcp
does not replace any of it. berserk-mcp sits next to Berserk and adds the
agent-facing surface:

| Capability | Default Berserk | berserk-mcp |
|---|---|---|
| Ingest OTLP logs / metrics / traces | ✅ core | reuses |
| KQL query engine + storage | ✅ core | reuses (read-only) |
| Web UI + `bzrk` CLI for humans | ✅ core | reuses |
| Token auth, profiles | ✅ core | reuses (`bzrk` holds the token) |
| **MCP surface for LLMs / agents** | — | ✅ |
| **Common questions answered without authoring KQL** | requires correct Kusto → small models fail | ✅ fixed verified tools |
| **Role-aware tool filtering** (SRE / SOC / Claude / Ops lanes) | — | ✅ `BERSERK_MCP_ROLE` env var |
| **Role primers** injected at `initialize` | — | ✅ KQL rules, thresholds, routing guidance per lane |
| **Telemetry-shape discovery** | partial (`.show tables`) | ✅ `list_metrics` · `discover_schema` · `container_hosts` |
| **Custom-query persistence** as named, reusable tools | UI has a Query Library. Berserk documents no API or CLI verb to create, list, or share a saved query programmatically | ✅ `save_query` (verify-before-persist) → `run_saved`, agent-readable |
| **Automated source onboarding** | — | ✅ `request_discovery` → worker → saved query, no KQL authoring needed |
| **LLM parser factory** — detect a new source, auto-author + verify a KQL query pack | — | ✅ `detect_new_sources` · `generate_parser` · `run_discovery_worker` · `review_generated` (ASIM-agent-style; see [below](#parser-factory-llm-generated-query-packs)) |
| **Query changelog / amendments log** | — | ✅ every `save_query` write tracked; `--worker` posts a Discord diff if [alerting is configured](#configuring-discord-alerts) |
| **Two-lane cost model** (cheap default · on-demand `@deep`) | — | ✅ tool descriptions + annotations make this safe |
| **KQL-injection guards** on free-text inputs | n/a (humans) | ✅ service-name allowlist · `claude_search` reject-list |
| **Trace/span analysis** — find slow/failed traces, reconstruct a span tree with correlated logs | — | ✅ `trace_find_slow` · `trace_find_errors` · `trace_analyze` (v1.14.0; see [Trace tools](#trace-tools-all-lanes)) |
| **Knowledge-artifact lifecycle pipeline** (source URL → validated skill artifact) | — | ✅ `canonloom_run_pipeline` · `canonloom_list_artifacts` · `canonloom_get_artifact` · `canonloom_freshness_report` · `canonloom_run_history`, bridged to a separate `canonloom-server` (see [CanonLoom bridge](#canonloom-knowledge-artifact-lifecycle-bridge)) |

### Why this complements Berserk's native MCP (not competes with it)

Berserk ships its own MCP server, `bzrk mcp`. It is a raw query console:
`query`/`start_query` sessions, table/database discovery, and `get_docs` for
KQL reference. Its design assumes the agent writes the KQL itself. That
console suits a skilled human KQL author well. It is the wrong everyday
interface for most models — they guess table names, botch aggregations, and
waste tokens on retries.

berserk-mcp is a **deterministic layer on top of the same backend**. The
model picks a verified intent. berserk-mcp does the math. The answer comes
back as a conclusion — a verdict, a baseline deviation, a cost trend — not a
row dump.

Use the native MCP server when a skilled KQL author drives the session. Use
berserk-mcp when you want *any* model, including small local ones, to answer
reliably. Both servers run side by side in the same client without
conflict.

### Sovereign and defense deployments (fully local stack)

Every layer of this stack can run on hardware you own, with zero cloud
egress. This makes it suitable for sovereignty-constrained and defense or
air-gapped environments:

- **Berserk** is self-hosted. Telemetry never leaves your network.
- **berserk-mcp** is pure Python stdlib. It has no third-party packages, no
  telemetry, and no phone-home. You can audit its five small files in an
  afternoon.
- **The LLM layer can run locally too.** The parser factory's provider
  ladder speaks the OpenAI-compatible API. Any locally hosted open-weight
  model — through Ollama, llama.cpp, vLLM, or LM Studio — plugs in as the
  `hermes` endpoint. No frontier API is required. The fixed-query design
  also lowers the skill the model needs: it only picks a tool and a time
  window. It never authors KQL.
- **Defense-in-depth on the egress path.** Even when an LLM endpoint is
  configured, it receives only structural telemetry — key names, shapes,
  redacted excerpts — never raw values. The endpoint URL is
  scheme-allowlisted and operator-controlled.

**What we've actually verified about self-hosted model viability**, not just
asserted (full methodology and data:
[docs/model-routing-cost-validation-2026-08-23.md](docs/model-routing-cost-validation-2026-08-23.md)).
This corrects earlier guidance in this section. That guidance claimed
"small local models route reliably." Measured against this server's real
70-tool schema, that claim did not hold. A reader building a sovereign
deployment needs the accurate number, not the hopeful one:

- **7-8B local models are not viable.** Qwen2.5:7b and Llama3.1:8b, tested
  locally via Ollama against the real schema, scored 5-7% tool-selection
  accuracy — far below a simple keyword-matching baseline (66%). Public
  tool-calling benchmarks (BFCL) rank these families well. But those
  benchmarks measure a much smaller tool count. They do not predict
  performance reliably at this schema size (see the now-superseded
  shortlist in [`evals/model-eval-plan.md`](evals/model-eval-plan.md)).
- **The measured reliability floor is the ~24B parameter class**, and one
  real candidate at that tier is confirmed genuinely self-hostable under an
  open license: `mistral-small-3.2-24b-instruct` (Apache 2.0, confirmed on
  Hugging Face, ~55GB GPU RAM at bf16) reached 78% tool-selection accuracy
  in the same eval. Its numbers come from the same OpenRouter-hosted eval as
  every other candidate, not from an actual local deployment — self-hosted
  latency and behavior on that specific hardware profile is not yet
  independently verified.
- One size class down (`mistral-nemo`, ~12B) scored *below* the
  keyword-matching baseline — smaller is not a safe default assumption
  anywhere in this range.
- **Bottom line for a sovereign deployment today:** budget for a ≥24B-class
  open-weight model and a GPU, not a small one. A fully local setup with a
  genuinely small (7-8B) first tier is not yet a validated configuration
  against this server's tool count.

#### Bridging Berserk's two use cases: AI Ops without leaving the sovereign boundary

Berserk's own positioning splits into two cases.
[AI Ops](https://www.bzrk.dev/use-cases/ai-ops/) says "any MCP-aware agent can
query your telemetry directly." [Defence](https://www.bzrk.dev/use-cases/defense/)
says "nothing leaves the trust boundary you control." Taken separately,
these two cases conflict. The AI Ops case assumes a capable model that
authors KQL and reasons over raw results. But a frontier model is itself an
egress dependency, and the Defence case rules that out. Berserk itself
solves this for the *data*: self-hosted, WORM storage, no foreign
jurisdiction. It does not solve this for the *reasoning layer* on top of
the data.

berserk-mcp closes that gap. The model only ever picks a tool and a time
window. It never authors KQL and never sees raw values. This means a small,
locally hosted open-weight model can drive the whole interaction reliably.
The result is the AI Ops experience — agents ask questions instead of humans
reading dashboards — with the entire agent loop inside the sovereign
boundary, not just the telemetry store.

This is not hypothetical. One production-like deployment uses a
Discord-facing local agent to answer on-call questions against Berserk. The
agent logs every tool call and every full prompt/reply back into Berserk
itself: model name, redacted arguments, redacted results, and session ID —
all as structured, queryable records. Berserk's AI Ops page highlights this
same durable, back-testable "what did the agent actually do" record in its
Ethira governance case study. Here it runs end-to-end against berserk-mcp
instead of a bespoke integration. The `claude_*` tool family
(`claude_cost_report`, `claude_token_burn`, `claude_workflow_insights`, and
others) delivers the same token-usage/BI story from that page. These tools
are already implemented and already answering real queries.

The target operating model is **two-tier local**. A locally hosted
open-weight model at the measured reliability floor (~24B class — see
above) handles the everyday calls; the goal is ≥ 80% of interactions. The
model escalates to a larger, locally hosted open-weight model only for
`@deep` work: parser generation, deep-dive synthesis, incident narratives.
"Small" here means the smaller of the two local tiers, not a 7-8B model —
those were tested and are not reliable enough to anchor either tier. The
escalation mechanism itself (a policy deciding when to route up)
is implemented and tested against a cloud-hosted small/deep pair in
`evals/escalation_policy.py`; picking and verifying the actual local model
for each tier against real hardware is still open. The original
measurement plan is in
[`evals/model-eval-plan.md`](evals/model-eval-plan.md) (Part 3) — read its
Part 1 benchmark shortlist skeptically; it predates the real eval above and
its picks did not hold up at this server's actual tool count.

---

## Architecture

### How the lanes talk to each other and to Berserk

```mermaid
flowchart LR
  classDef user      fill:#0d1117,stroke:#58a6ff,color:#c9d1d9
  classDef mcp       fill:#161b22,stroke:#8b949e,color:#c9d1d9
  classDef security  fill:#3a0d0d,stroke:#f85149,color:#c9d1d9
  classDef berserk   fill:#1d1d3a,stroke:#a371f7,color:#c9d1d9

  User([User / agent · Claude Code, Claude Desktop, etc.]):::user

  subgraph M["berserk-mcp (stdio · zero-dep Python)"]
    direction LR
    Tools["Tools\nquery · discover · learn"]:::mcp
    Redact["Secret / PII filter\nruns on every response"]:::security
    Tools --> Redact
  end

  Bzrk["bzrk CLI\nholds the bearer token"]:::berserk
  Gw[("Your Berserk instance\nKQL engine + storage")]:::berserk

  User -- "ask a question" --> Tools
  Tools -. "argv, no shell" .-> Bzrk
  Bzrk -- "read-only, bearer auth" --> Gw
  Redact -- "filtered answer" --> User
```

Two things worth knowing about this diagram:

1. **The bearer token never enters this code.** `bzrk` owns the token in its own configuration. berserk-mcp invokes it with an argv list: no shell, no token in berserk-mcp process memory, no token in berserk-mcp logs. Private-file permissions are platform-specific — see [Security](#security).
2. **Every tool response passes through the secret/PII filter before reaching a model.** It fails closed: if the redaction mode is unset, it defaults to the safest setting (`redact`) rather than passing text through unfiltered.

This diagram covers the core, always-on path. berserk-mcp also bridges
optionally to CanonLoom — a separate project, reached over plain HTTP,
purely opt-in via `CANONLOOM_SERVER_URL`. Every `canonloom_*` tool checks
that variable at call time and returns a clear configuration error if it's
unset; nothing in the diagram above requires it to be running.

This is the high-level picture. The cheap/deep model split, the
learning-loop cache, the discovery worker, role filtering, and transport
options are covered elsewhere in this README and in the tool descriptions
themselves.

### Example ingestion topology (not shown in the diagram)

The diagram above covers the **query path**: how an agent asks questions.
The **ingestion path** is separate. A typical deployment runs a lightweight
journal forwarder on each monitored host. It tails explicitly selected
services and ships OTLP log payloads through a local collector into the
Berserk gateway. Each service uses its own
`resource['service.name']`, so `list_services`, `logs_for_service`, and
`search` filter by the workload rather than the forwarding mechanism. Keep
real host and service inventories in private deployment documentation.

---

## Role lanes

Set `BERSERK_MCP_ROLE` to scope what an agent sees. The filter applies at the
MCP protocol level. An unrelated tool never appears in `tools/list`, so it
cannot be called by accident and cannot be injected into context.

| Role | `BERSERK_MCP_ROLE` | Gets | Typical agent |
|---|---|---|---|
| SRE | `sre` | Core tools + SRE tools (error rate, host headroom, ingest health, service health, top errors) | On-call Slack bot, editor assistant |
| SOC | `soc` | Core tools + SOC tools (high-severity logs, log spike, new services, repeated errors, incident timeline) | Security monitoring agent |
| Claude Code | `claude` | Core tools + Claude telemetry, AI spend, feature economics, data quality, and governed harness recommendations | Developer workflow and AI FinOps assistant |
| Ops | `ops` | All tools (full visibility) | Operator shell, admin scripts |
| Default | `all` (or unset) | All tools | Development, evaluation |

### Role primers

When a lane connects, berserk-mcp injects a markdown primer into the MCP
`initialize` response, before the standard instructions. Each primer carries:

- **Tool routing table** — which tool to reach for first, for each intent
- **Escalation thresholds** — for example CPU load > 2.0, memory > 85%, error rate > 10/min, ingest lag > 30 s
- **KQL authoring rules** — time window defaults, field name conventions, aggregation patterns
- **Discovery flow guidance** — when to call `request_discovery` instead of authoring ad-hoc KQL

This means the agent config needs no prompt engineering. The routing
knowledge travels with berserk-mcp.

Primers live in `primers/<role>.md`, next to the server file. An explicit
`BERSERK_MCP_PRIMERS_DIR` must be absolute and contain a readable `<role>.md`
for the active lane; otherwise startup fails with a configuration error. The
`all` role receives no primer and routes from tool descriptions directly.

---

## Tools

### Core tools (all lanes)

| Tool | What it answers |
|---|---|
| `list_containers` | Containers currently sending metrics (with sample counts). |
| `top_cpu` | Containers ranked by CPU %. Use for container-specific questions; for host CPU use `host_cpu`. |
| `top_memory` | Containers ranked by memory (MB). Use for container-specific questions; for host memory use `host_memory`. |
| `errors_by_service` | ERROR-level log counts grouped by service. |
| `list_services` | All services/sources, with log vs metric breakdown. |
| `list_hosts` | All hosts reporting telemetry, by record count. |
| `host_cpu` | Per-**host** CPU (1-minute load average). Default for ambiguous whole-machine CPU questions. |
| `host_memory` | Per-**host** memory used (GB). Default for ambiguous whole-machine memory questions. |
| `container_hosts` | Which host/VM each container runs on (join key for container↔host questions). |
| `logs_for_service` | Recent log lines for one service. |
| `schema` | Live tables + column schema introspection. |
| `list_metrics` | Every metric name being ingested, with counts (discovery). |
| `discover_schema` | Field metadata (type, cardinality, representative values) via Berserk's native `fieldstats`, plus a structural presence sample, to learn an unknown source without exporting raw telemetry (v1.17.0; previously `bag_keys`-based). |
| `validate_kql` | Validate custom KQL before saving or running it. Static mode checks syntax shape, schema fields, bounds, and cost-risk without executing the query; live mode is opt-in and returns a runtime receipt when enabled. |
| `bzrk_query_perf` | Berserk query engine latency percentiles (p50/p95/p99 in µs). |
| `search` | Run arbitrary KQL (escape hatch). Static validation runs before execution in the default `warn` mode. Save the result with `save_query` once it works. Fields are nested `resource`/`attributes`, not flat columns — for example `resource['service.name']`, not `service_name`. Call `discover_schema` first if you don't know the field names for a source. |

Every query tool takes an optional `since` argument (`"15m ago"`, `"1h ago"`,
`"2d ago"`, …) with a sensible per-tool default.

**Per-host vs. per-container:** `host_cpu` and `host_memory` report per **host**. `top_cpu` and `top_memory` report per **container**. The tool descriptions cross-reference each other, so the model picks the right one. For an ambiguous whole-machine question — for example "what's hammering the server?" — always prefer the host tools.

### SRE tools (`sre` lane only)

| Tool | What it answers |
|---|---|
| `sre_error_rate` | Error log events by service grouped per minute — "is the error rate climbing?" |
| `investigate_error_rate` | Fixed decision-tree root-cause walk for an elevated error rate — errors_by_service → correlated log-spike → failing traces, one hop per call. |
| `sre_host_headroom` | CPU load and memory by host — "which VM is saturated?" |
| `sre_ingest_health` | Berserk ingest lag and dropped data — "is observability lagging?" |
| `sre_service_health` | Full health summary for one named service: event volume, error count, log/metric split, last seen. |
| `sre_top_error_messages` | Most-repeated error messages by service — "what error should I investigate first?" |
| `detect_anomalies` | Statistical service-volume anomaly detection using zero-filled series. |
| `forecast_capacity` | Native trend fit for an allowlisted host gauge; refuses weak forecasts. |

### SOC tools (`soc` lane only)

| Tool | What it answers |
|---|---|
| `soc_high_severity_logs` | Recent CRITICAL/FATAL log lines with service and message text. |
| `soc_log_spike` | Services with the largest minute-level log bursts — "anything spiking?" |
| `soc_new_services` | Recently first-seen services and sources — "what is new?" |
| `soc_repeated_errors` | Error messages that repeat persistently — probes, loops, stuck processes. |
| `soc_timeline` | Full incident timeline for one named service: timestamps, severity, metric names, message snippets. |
| `detect_anomalies` | Statistical service-volume anomaly detection using zero-filled series. |
| `find_similar` | Meaning-based log search when semantic indexing is enabled. |
| `scan_secrets` | Aggregate potential-secret counts by service/type with first-seen timestamps. Values are never returned. |

### Claude Code tools (`claude` lane only)

If you ship Claude Code session logs into Berserk (service name `claude-code`), these
tools mine that data. See [docs/claude-code.md](docs/claude-code.md) for the pipeline.

| Tool | What it answers |
|---|---|
| `claude_recent` | Recent Claude Code events — type, role, model, tool names, error flag. |
| `claude_sessions` | Sessions rollup — event counts, first/last seen, assistant turns, tool turns, error count. |
| `claude_tools` | Tool-use histogram — how many times each tool (Bash, Edit, Read, …) was called. |
| `claude_errors` | Failed tool results with message snippets. |
| `claude_search` | Full-text search across Claude Code message and tool bodies. |
| `claude_quota_status` | Live quota-window check: reads Anthropic's account-usage endpoint when available (macOS only), falling back to a log-derived token estimate over the trailing window otherwise. Doesn't require the ingestion daemon running. |
| `claude_loop_check` | Flags sessions that repeat the same tool/target, retry the same error, or oscillate between calls. |
| `claude_model_fit` | Heuristic model-tier fit: frontier model on trivial work, or cheap model on complex/repetitive work. Not a billing statement. |
| `claude_token_burn` | Token burn per session and progress unit, using exact usage attributes when present and a labeled estimate otherwise. |
| `claude_cost_report` | Multi-day cost report: per-day burn with exact/estimated labels, per-model split, optional per-project attribution from file paths, and a burn-growing/flat/declining trend verdict backed by Berserk's native `series_fit_line` (reports R², v1.17.0). |
| `claude_session_deep_dive` | One session's timeline: contiguous tool phases with error counts, activity gaps over 5 minutes, cumulative burn, and a loop verdict. |
| `claude_workflow_insights` | Cross-session patterns: most common tool sequences, error hotspots by tool+target, top-decile burn-per-target sessions. |
| `claude_spend_overview` | Token classes, public API-equivalent spend, cache ratio, trends, attribution, and pricing coverage grouped by business or technical dimension. |
| `claude_feature_cost` | Planned/actual developer hours and AI budget/spend, completion forecast, repositories, agents, harnesses, and delivery outcomes for one feature. |
| `claude_project_economics` | Feature and repository economics within one project, including unattributed spend and data-quality coverage. |
| `claude_efficiency_insights` | Evidence for expensive models, operations, retries, loops, cache misses, context growth, and agent fan-out. |
| `claude_harness_recommendations` | Deterministic, stable-ID harness amendments with confidence, risk, validation window, and rollback criteria. |
| `claude_record_recommendation_decision` | Append-only approval, rejection, or deferral audit record. Owners use a deployment-scoped HMAC pseudonym; rationale is stored as a hash. |
| `claude_optimization_impact` | Matched before/after harness comparison with keep, rollback, no-change, or insufficient-evidence verdict. |
| `claude_management_report` | Portfolio, project, or feature summary as readable Markdown plus versioned structured JSON. |
| `claude_generate_dashboard` | Privacy-safe Markdown or self-contained HTML snapshot beneath the configured report directory. |

`claude_recent`, `claude_sessions`, `claude_tools`, `claude_errors`, and
`claude_search` accept an optional `agent` parameter (default
`claude-code`) to query a different ingested agent's data instead — for
example `agent="codex-cli"`. Every other tool in this table is still
Claude-Code-specific.

### Agent-log intelligence

A read-only analytics layer for the `claude` lane (v1.12.0; see
[release notes](docs/releases/v1.12.0.md)):

- `claude_loop_check` groups tool calls by session. It reports the repetition ratio, the top repeated call, the error-retry count, and a verdict: `healthy`, `some-repetition`, or `likely-looping`.
- `claude_model_fit` maps model names to a coarse tier (`frontier`, `mid`, `cheap`). It compares that tier to a complexity proxy built from tool count, errors, duration, and loop signals.
- `claude_token_burn` uses `claude.tokens_input` and `claude.tokens_output` when present. When they are absent, it falls back per session to `body characters / 4`. It computes burn per distinct tool plus inferred file target, and highlights top-decile burn. Every result labels its source as exact or estimated.
- `--agent-report` runs all three checks headlessly. It exits non-zero when a session is likely looping or underpowered, so cron or systemd can pipe the stdout summary to an alert transport. "high-burn" alone is a relative marker — it is always present, because it is a top-decile ranking — so it is intentionally excluded from the alert threshold:

```bash
berserk-mcp --agent-report --since "6h ago"
berserk-mcp --agent-report --agent-report-mode weekly --agent-report-json --since "7d ago"
```

**Phase J deep analytics (v1.15.0; see [release notes](docs/releases/v1.15.0.md)):**
`claude_cost_report`, `claude_session_deep_dive`, and `claude_workflow_insights`
extend this layer with multi-day cost trends, per-session timeline
drilldowns, and cross-session workflow patterns. Per-project cost
attribution infers a project name from file-target paths: it uses the
directory before the first marker segment (`src`, `tests`, `lib`, `pkg`).
Override this with `BERSERK_MCP_PROJECT_MARKERS`.

`claude_token_burn`, `claude_loop_check`, and `claude_model_fit` parse real
`bzrk --json` output directly — `_json_records()` unwraps `Tables[0].rows`
against `Tables[0].schema.columns`, matching each row's positional array to
its column order. `claude.tokens_input` and `claude.tokens_output` are the
real attribute names used for exact token counts. See
[the v1.14.1 release notes](docs/releases/v1.14.1.md) for the silent-failure
bug this fixed and the live-verification story behind it.

### Secret detection and output redaction

A stdlib-only secret scanner at the MCP output boundary (v1.12.0; see
[release notes](docs/releases/v1.12.0.md)). `BERSERK_MCP_REDACT` controls
how every `tools/call` result is handled:

- `redact` (default since F-009, 2026-07-20) replaces detected values with typed placeholders, such as `[REDACTED:aws_key]`.
- `flag` leaves the result intact and prepends a warning when a secret is detected. This is an explicit opt-in away from the safer default. berserk-mcp logs a startup warning to stderr when you set this.
- `off` disables output scanning entirely. This is also an explicit opt-in, with a startup warning.

An unrecognized `BERSERK_MCP_REDACT` value fails **closed** to `redact`, the
strictest mode, never to a weaker one.

The scanner recognizes common cloud/provider credentials, private keys,
JWTs, bearer tokens, and generic password/token assignments. High-entropy
matching is opt-in, because it is false-positive-prone. Email, IP, and
Luhn-validated credit-card checks are each individually selectable.
`scan_secrets` audits recent log bodies but returns only aggregate counts and
timestamps; it never returns the matched values. This protects MCP output
only. You must still remove secrets already stored in Berserk at ingest, and
rotate any exposed credentials.

### Learning loop tools (all lanes)

| Tool | What it answers / does |
|---|---|
| `list_saved` | List saved queries visible to the current role. Check here before authoring new KQL. |
| `run_saved` | Run a saved query by name — deterministic, no KQL authoring. |
| `save_query` | Verify a KQL query runs, then persist it under a name (with optional role tag). Logs every write to the amendments log. |

### Ingestion advisor

`suggest_ingestion` is an all-lane read-only tool (v1.12.0; see
[release notes](docs/releases/v1.12.0.md)), backed by the editable
`ingestion_catalog.json` knowledge base. The tool recommends concrete
sources, explains why each source matters, names an ingestion mechanism,
and labels its maturity: `turnkey`, `collector-receiver`,
`bridge-required`, or `manual`.

Seeded use cases:

- `sre/aws-cloud-native`
- `sre/azure`
- `sre/onprem-ad-health`
- `soc/endpoint-identity`
- `change-management/ansible`
- `scom`

Set `check_gap=true` to compare service and metric hints with the live
Berserk inventory. Each recommendation is marked `present` or `missing`, with
the matching signal or the exact ingestion action. For example:

```text
suggest_ingestion role_or_usecase=sre/onprem-ad-health check_gap=true
```

The AD path recommends Security, System, and Directory Service channels
through the OTel Collector `windowseventlog` receiver. The Ansible path uses
the `community.general.opentelemetry` callback. SCOM is explicitly
`bridge-required`: it needs a read-only REST/API or warehouse-SQL-to-OTLP
bridge. The advisor does not claim a native SCOM OTel receiver exists.

### Discovery tools (all lanes)

| Tool | What it does |
|---|---|
| `request_discovery` | Queue a newly-added service or metric for automated onboarding. Validates the source exists in Berserk before accepting. |
| `discovery_status` | List pending and completed discovery jobs. |

### Just-in-time tool discovery (`find_tool`, opt-in)

Not to be confused with the telemetry-*source* discovery tools above —
this is discovery over berserk-mcp's own *tool catalog*.

| Tool | What it does |
|---|---|
| `find_tool` | Search-by-intent over the full tool catalog. Returns the best-matching candidates with their complete `inputSchema` inline, so a model can call a tool it was never shown up front. |

Set `BERSERK_MCP_DISCOVERY=1` to switch from listing the full tool catalog
up front to exposing 8 fixed anchor tools plus `find_tool` as the entry
point for everything else (v1.26.0, issue #14). Measured against a real
MCP handshake: the full schema costs ~17,560 tokens; discovery mode costs
~1,386 — a 92% reduction. A recall-gate test
(`tests/test_tool_discovery.py`) requires every shipped tool to be
reachable by at least one realistic phrasing before it ships; current
measured recall is 100% across 210 phrasings covering all 70 tools. Off by
default — every tool stays directly listed unless you opt in. See
[Choosing a model](#choosing-a-model) for why this matters most for
smaller models.

### Trace tools (all lanes)

| Tool | What it answers |
|---|---|
| `trace_find_slow` | Highest-duration root spans in the time window — "what's slow?" Entry point before `trace_analyze`. |
| `trace_find_errors` | Spans whose status indicates an error — "which requests failed?" Entry point before `trace_analyze`. |
| `trace_analyze` | Full breakdown of one trace by `trace_id`: every span in time order, plus correlated log lines sharing the same `trace_id`. |

Distributed-trace analysis (v1.14.0; see
[release notes](docs/releases/v1.14.0.md)), following this table's
`<signal>_name` field convention (`metric_name` for metrics, `body` and
`severity_text` for logs). This feature was ported from a separate
TypeScript MCP prototype that explored the same problem space.
These tools are verified against a real Berserk cluster whose own internal
services are self-instrumented — `service=query`, `service=gateway`, and
`service=ingest` spans are real trace/span data, not synthetic test
fixtures (see [Live-verified, not just unit-tested](#live-verified-not-just-unit-tested)).

Two design points worth knowing:

1. **`duration` is a *dynamic*-typed column.** Berserk's KQL engine rejects `sort by duration` directly. `trace_find_slow` casts it with `toint(duration)` before sorting.
2. **Not every row sharing a `trace_id` is a span.** Other correlated telemetry — for example a log row — can carry the same `trace_id`/`span_id` with a null `span_name`. `trace_analyze` filters to `isnotnull(span_name)`, and sorts by `start_time` so parent spans order correctly before their children.

(Both were live bugs found while verifying this feature against a real
cluster outage — see the release notes for the full story.)

### Native analytics and graceful degradation

`detect_anomalies` and `forecast_capacity` (v1.18.0; see
[release notes](docs/releases/v1.18.0.md)) use Berserk's native series
functions, returning compact arrays instead of exporting raw event windows.
Forecast responses include R² and slope; trends with R² below 0.6 or a
non-positive slope are explicitly reported as not forecastable rather than
inventing a ceiling date.

`find_similar` depends on semantic indexing and the `similarto` parser
feature. On clusters where that feature is unavailable, the tool does not
fail open or pretend exact matching is semantic — it explains the
limitation and directs the caller to `search` with an exact `has` term.

---

## Cost & BI reporting

berserk-mcp also ships a separate cost and attribution layer. It runs
through CLI flags and a wrapper binary (`berserk-claude`), not `tools/call`.
Native Claude Code OpenTelemetry is the preferred input. Reports normalize
input, output, cache-read, cache-creation, long-context, and chargeable
server-tool usage into one versioned, public-API-equivalent cost. This is
not an invoice. An unknown model stays unpriced rather than getting a
guessed rate. You can launch Claude with governed work context, so
telemetry attributes to a feature without exposing prompts or source code.
You import planning and actuals through a neutral CSV/NDJSON contract. You
export management-ready BI datasets and dashboards from the same model.
Generated outputs contain aggregates and coverage metadata only — never
prompts, code, or cleartext owner IDs.

Full CLI reference (flags, business-data record shapes, export/dashboard
format, privacy/permission details):
[docs/cost-and-bi-reporting.md](docs/cost-and-bi-reporting.md).

---

## Self-extending: discovery and learning

The fixed tools cover known telemetry. For data with no tool yet — a log
source you just started shipping — a two-stage loop extends berserk-mcp
without hand-editing code. The cheap lane stays deterministic throughout.

### Stage 1: Discovery queue

```
QUEUE    request_discovery(service="haproxy")   →  validates source, queues job
WORKER   discover-worker drains queue at 06:00  →  authors KQL by role/kind
SAVE     save_query (verify-before-persist)      →  permanent, named query
REUSE    run_saved("sre_haproxy_service")        →  cheap model, free, forever
```

`request_discovery` does one check before it accepts a job: it calls
`list_services` (or `list_metrics`) to confirm the source is actually
visible in Berserk. An unknown source is rejected with a clear error, so the
queue never fills with phantom jobs.

The **discover-worker** (`berserk-mcp --worker`, invoked from a daily cron
entry — there is no separate `discover-worker.py` file) drains the queue:

- Chooses the right KQL template per role. `sre` gets a health summary, `soc` gets an incident timeline, `claude` gets a health rollup, and `metric` kind gets a drilldown aggregation.
- Calls `save_query` to verify and persist the result.
- Updates `known_sources.json` so the same source is never re-queued.
- Posts a summary of completed and failed jobs to Discord, if `BERSERK_DISCORD_ALERT_SECRET` is configured (see below). This step is skipped when there is nothing noteworthy — no new sources found and no jobs drained — so a quiet day does not generate a daily ping.

### Stage 2: @deep amendments and improvements

A capable model (`@deep`, a scheduled agent, or an operator) may improve or
correct an existing query via `save_query`. The generation pipeline may also
save a new query. Either way, berserk-mcp:

1. Tags the entry `action=generated` (pipeline-authored), `action=updated` (a human save to an existing name), or `action=created` (a human save to a new name).
2. Appends a timestamped entry to `amendments_log.json`, with the name, description, KQL preview, role, and action.
3. Reads and formats a changelog on the next `--worker` run, if Discord alerting is configured (🤖 generated, ✏️ updated, ✨ created). It clears the log **only if the post is confirmed** — a transient Discord outage leaves the entries intact for the next run, instead of losing them.

This means **the query store is auditable**. Once Discord alerting is
configured, every improvement made by an autonomous agent can be surfaced in
a Discord channel automatically, with no operator action.

#### Configuring Discord alerts

berserk-mcp does not talk to Discord's API directly. No bot token and no
webhook secret lives in this process. Instead, berserk-mcp posts to a small
local HTTP bridge (loopback by default) that already knows how to reach your
Discord channel:

| Variable | Default | Purpose |
|---|---|---|
| `BERSERK_DISCORD_ALERT_URL` | `http://127.0.0.1:8765/alert` | The bridge's alert endpoint. |
| `BERSERK_DISCORD_ALERT_SECRET` | unset | Shared secret sent as `X-Auth-Token`. **Alerting is entirely off unless you set this** — no default secret, no silent posting. |

The bridge must accept `POST <url>` with header `X-Auth-Token: <secret>` and
JSON body `{"text": "..."}`, and return 2xx on success. If the bridge runs on
a different host than berserk-mcp's `--worker` cron job, the same
loopback-only-by-default policy applies as for the LLM endpoint. Set
`BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE=1` to allow a non-loopback `http://` URL,
or point at an `https://` bridge instead. Prefer HTTPS for any bridge that is
not bound to loopback; the shared secret is sent as an HTTP header and should
not cross an unencrypted network. Alerts are sent only from the
headless `--worker` CLI path. Interactive MCP tool calls (for example
`run_discovery_worker`) already surface their result directly to the caller
and never post to Discord — this avoids duplicate, noisy notifications.

The intended division of labour is cost-efficient:

- **A capable model does the rare, hard part.** It discovers the new shape, authors and verifies the query, and calls `save_query`. Trigger it two ways: on a **schedule** (a daily job that checks the discovery queue), or **on demand** ("I just added HAProxy to Berserk — add support").
- **The cheap model reaps the result.** Every saved query is reusable for free, deterministically, via `run_saved`. Authoring KQL is the one thing small models handle badly, so this step is gated behind the stronger model. `save_query` verifies the query runs before persisting it, as a guardrail.

This design scales because **learned queries live behind
`list_saved`/`run_saved`**, not as first-class tools. You can learn dozens of
new sources without growing the routing surface that keeps the cheap model
reliable.

---

## Parser factory: LLM-generated query packs

When a new source starts shipping to Berserk with no tool for it yet, the
parser factory automates what a human would otherwise do by hand:
`discover_schema`, hand-write KQL, `save_query`. Following the design of
Microsoft's [ASIM parser AI agent](https://learn.microsoft.com/en-gb/azure/sentinel/normalization-create-parsers-ai-agent)
for Sentinel, it samples the source, generates KQL, validates by executing
it, refines on failure (capped at 5 cycles per provider), and persists only
the verified survivors as a reusable **query pack** — 2-4 saved queries per
source. Tries cheap/local providers first, has hard runaway fail-safes
(per-run caps on both queuing and generation), and never lets a generated
query silently overwrite a human-saved one.

Tools: `detect_new_sources`, `generate_parser`, `run_discovery_worker`,
`review_generated`. Full pipeline mapping, configuration reference,
headless/cron mode, and safety details:
[docs/parser-factory.md](docs/parser-factory.md).

---

## CanonLoom: knowledge-artifact lifecycle bridge

The parser factory (above) turns new *telemetry sources* into verified KQL.
CanonLoom solves the analogous problem for *knowledge sources*: turning a
source URL into a validated, versioned skill artifact through a five-phase
pipeline (CLP-1 through CLP-5) with a hard validation gate before anything
is trusted. **CanonLoom is a separate project**, not part of berserk-mcp —
it ships its own HTTP API server (`canonloom-server`) and knowledge
repository; berserk-mcp only bridges to that API via five tools, with zero
shared dependencies. Run berserk-mcp with no `canonloom-server` anywhere and
everything else works exactly as documented above; only the `canonloom_*`
tools return a clear setup error instead of a result.

Tools: `canonloom_run_pipeline`, `canonloom_list_artifacts`,
`canonloom_get_artifact`, `canonloom_freshness_report`,
`canonloom_run_history`. Deployment diagram, pipeline-phase reference,
configuration, and worked examples:
[docs/canonloom-bridge.md](docs/canonloom-bridge.md).

---

## Worked examples

Concrete prompts you can paste into any MCP-aware client. Each example shows
the natural-language question, the tools the model calls, and the kind of
answer you get. All of these work on the cheap default lane — no frontier
model required.

### ChatOps: "any errors in the last hour?" (SRE lane)

```
Have there been any errors in the last hour, and from which service?
```

> Calls `errors_by_service` (`since="1h ago"`). The model replies with the
> per-service error count, or "no errors recorded" when the result is empty.
> On the SRE lane, the primer nudges the model toward `sre_error_rate` for a
> time-series view when the count is above threshold.

### On-call triage: "is api-gateway healthy?" (SRE lane)

```
Is api-gateway healthy? What's the error rate and when was it last seen?
```

> Calls `sre_service_health(service="api-gateway")`. It returns total events,
> error count, log/metric split, and the last-seen timestamp in one round
> trip. If the error count is high, the primer's threshold guidance nudges
> the model to follow up with `sre_top_error_messages`.

### SOC investigation: "what happened on otel-collector?" (SOC lane)

```
Reconstruct what happened with otel-collector over the last 2 hours.
```

> Calls `soc_timeline(service="otel-collector", since="2h ago")`. It returns
> timestamped events with severity, metric names, and message snippets,
> ordered newest-first — a ready-made incident narrative, with no KQL
> authoring.

### Security sweep: "anything new or anomalous?" (SOC lane)

```
Anything unusual in the last 30 minutes? Spikes, new sources, repeated errors?
```

> Calls `soc_log_spike`, `soc_new_services`, and `soc_repeated_errors` in one pass.
> The SOC primer tells the model to scan all three before summarising.

### Developer workflow: "what tools is Claude Code using?" (Claude lane)

```
What tools has Claude Code used most this week, and were there any errors?
```

> Calls `claude_tools(since="7d ago")` and `claude_errors`. This only works if
> you ship Claude Code session logs into Berserk via an OTLP forwarder — see
> [docs/claude-code.md](docs/claude-code.md).

### Onboarding a new source

```
I just added HAProxy logs to Berserk. Integrate it.
```

> (With `SOUL.md` or a system prompt configured.) The agent calls
> `request_discovery(service="haproxy", role_hint="sre")`. The discovery
> worker runs overnight. It authors and saves `sre_haproxy_service`. The
> next morning, `run_saved` answers HAProxy questions on the cheap lane,
> permanently.

### Autonomous daily health digest (cron / scheduled agent)

```
You are an on-call assistant. Use the Berserk MCP to:
1) Check load per host (host_cpu, host_memory) over the last 6 hours.
2) Count errors per service over the last 24 hours (errors_by_service).
3) List the top 5 noisiest containers (top_memory).
Write a 10-line digest, flag anything anomalous, and stop.
```

> This is deterministic enough to run unattended overnight on `gpt-4.1-mini`
> or a self-hosted ≥24B model — a 7-8B local model is not reliable enough
> for this (see [Choosing a model](#choosing-a-model)). Wire it to a cron
> job — the answer is short and parseable.

---

## Requirements

- Python 3.9+. (Python 3.8 reached upstream end-of-life on 2024-10-07 and is no longer a supported floor.)
- The [`bzrk`](https://docs.bzrk.dev) CLI, installed and authenticated (`bzrk -P <profile> search "..."` must work). The bearer token lives in `bzrk`'s own config. berserk-mcp never reads or stores it.
- *(Optional)* A running `canonloom-server` instance, only if you use the `canonloom_*` tools — a separate project with its own, stricter requirements; berserk-mcp only calls its HTTP API and adds nothing to berserk-mcp's own dependency footprint. Setup: [canonloom's README](https://github.com/ssimonsen0202/canonloom#running-canonloom-server).

## Install

berserk-mcp is not yet published to PyPI. Install from source:

```bash
git clone https://github.com/ssimonsen0202/berserk_mcp
cd berserk_mcp
pip install .
```

`pip install berserk-mcp`, `pipx install berserk-mcp`, and `uvx berserk-mcp`
will work once this project is published under that name. Do not run them
yet: the name `berserk-mcp` is currently unclaimed on PyPI, so those
commands would silently succeed against whatever unrelated or malicious
package claims it first.

berserk-mcp uses only the Python standard library. It has no third-party
runtime dependencies. Installation must include the accompanying local
modules declared in `pyproject.toml` plus packaged data (`primers/`,
`ingestion_catalog.json`). Use `pip install .` or a built wheel. Do not copy
`berserk_mcp.py` alone.

## Authenticate to `bzrk`

berserk-mcp does not talk to Berserk directly. It wraps the `bzrk` CLI.
Authentication is `bzrk`'s job, not berserk-mcp's. **The Berserk bearer
token lives only in `bzrk`'s own config. berserk-mcp never reads it,
stores it, forwards it, or logs it.**

Recommended one-time setup:

```bash
# 1. Log in to Berserk with the profile name you'll use from the MCP.
bzrk login          # follow the prompt for endpoint + token
# or
bzrk -P prod login  # log in to a specific named profile

# 2. Verify auth works with the same profile the MCP will use.
bzrk -P local search "default | take 1" --since "1h ago"

# 3. Point the MCP at that profile (or leave BZRK_PROFILE unset for `local`).
export BZRK_PROFILE=local
```

**Profiles.** Berserk uses named profiles (`local`, `prod`, `staging`, and
others). You can point the MCP at a different tenant by changing one env
var. `berserk-mcp` reads `BZRK_PROFILE` and passes it to every `bzrk`
invocation as `-P <profile>`. In `claude_desktop_config.json` this looks
like `"env": {"BZRK_PROFILE": "prod"}` — see [Connect it to a
client](#connect-it-to-a-client) below.

**Non-default `bzrk` binary.** If `bzrk` is not on `$PATH` — for example, if
it is Homebrew-installed or lives in a per-repo `.venv` — set `BZRK_BIN` to
the full path. `berserk-mcp` invokes `bzrk` with an argument list, never
through a shell, so quoting is not a concern. On Windows, use an absolute path
to the trusted executable. A bare name that resolves inside the MCP client's
current working directory is rejected to prevent executable planting.

**Auth failures at runtime.** If `bzrk` returns an authentication error —
bad token, expired session, wrong profile — berserk-mcp returns this
constant string:

```text
bzrk authentication failed; run `bzrk login` and retry
```

For authentication failures, berserk-mcp never propagates raw `bzrk` stderr,
tokens, or tenant identifiers to the caller. Other backend diagnostics can be
returned to the MCP caller, but they are bounded and pass through output
redaction; see [Security](#security) for the full rationale.

**Full `bzrk` auth options** (SSO, service accounts, per-profile config) are
out of scope for this README. See the official Berserk CLI docs at
<https://docs.bzrk.dev>. berserk-mcp only requires that `bzrk -P <profile>
search "..."` succeeds, from the same shell environment berserk-mcp will
run in.

## Fleet-friendly operation

When many MCP instances share one Berserk cluster, berserk-mcp limits the
load each instance contributes (v1.18.0; see
[release notes](docs/releases/v1.18.0.md)):

- Worker mode adds randomized startup jitter, preventing synchronized cron
  bursts.
- Interactive calls use a separate per-tool budget and return an actionable
  narrower-window message when the budget is exceeded.
- Identical timeout retries are suppressed briefly to prevent retry storms.
- Allowlisted read-only rollups use a short in-process cache. Cached
  results are marked `(cached, <age>s old)`; mutation, discovery,
  generation, and arbitrary-search tools are never cached.

These controls are per-process and can be disabled or tuned with the
environment variables in the configuration table below. Each default is a
measured value, not an arbitrary guess — see
[the v1.18.0 release notes](docs/releases/v1.18.0.md) for the evaluation
evidence behind each number.

## Configure

All configuration is via environment variables, all optional — covering
query/worker tuning, KQL validation policy, redaction and pseudonymization,
BI/report paths, OTLP export, and the optional HTTP transport. Full table
and defaults: [Configuration reference](docs/configuration.md).

Parser-factory (LLM parser generation) has its own env vars — see
[Parser factory](#parser-factory-llm-generated-query-packs) above.

The CanonLoom bridge has its own two env vars (`CANONLOOM_SERVER_URL`,
`CANONLOOM_API_KEY`) — see
[CanonLoom bridge](#canonloom-knowledge-artifact-lifecycle-bridge) below.

### Transport security guidance

Every non-loopback endpoint that carries a token, API key, or telemetry
payload (Hermes, Discord alerts, OTLP export, the optional HTTP MCP
transport, and the Berserk cluster endpoint itself) needs HTTPS/TLS, and
code enforcement already covers the endpoints berserk-mcp owns — schemes
allowlisted, embedded credentials and control characters rejected, no
redirect-following, bounded response bodies, and HTTPS required for remote
OTLP. Full per-endpoint guidance:
[Transport security and TLS guidance](docs/tls-transport-security.md).

## Connect it to a client

**Compatibility.** berserk-mcp implements MCP protocol version `2025-06-18`
as a stdio server (newline-delimited JSON-RPC 2.0). All 63 registered tools
appear in the `tools/list` handshake, and each can be invoked via
`tools/call`. The stdio handshake path — including every required
lifecycle method (`initialize`, `notifications/initialized`, `ping`,
`tools/list`, `tools/call`) — has been externally exercised by two
independent scanners: Cisco AI Defense `mcp-scanner` and MCP-Shield. Both
scanners enumerated the full tool surface with no protocol errors. Every
method has adversarial regression coverage in the test suite. Any client
that speaks the same protocol version — Claude
Desktop, Claude Code, and third-party MCP clients — can drive berserk-mcp
with no server-side changes.

### Claude Desktop

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "berserk-q": {
      "command": "berserk-mcp",
      "env": {
        "BZRK_PROFILE": "local",
        "BERSERK_MCP_ROLE": "sre"
      }
    }
  }
}
```

If you didn't `pip install` it, point at the file instead:

```json
{
  "mcpServers": {
    "berserk-q": {
      "command": "python",
      "args": ["/absolute/path/to/berserk_mcp.py"],
      "env": {
        "BZRK_PROFILE": "local",
        "BERSERK_MCP_ROLE": "sre"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add berserk-q -- berserk-mcp
# or from source:
claude mcp add berserk-q -- python /absolute/path/to/berserk_mcp.py
```

Set the role in your shell or `.env`:

```bash
BERSERK_MCP_ROLE=sre claude mcp add berserk-q -- berserk-mcp
```

### Any MCP client

Launch `berserk-mcp` (or `python berserk_mcp.py`) as a stdio MCP server. It speaks
newline-delimited JSON-RPC 2.0 over stdio, MCP protocol version 2025-06-18.

### Auditing tool calls from an agent-framework client

Some MCP hosts keep a full per-run session transcript on disk, including
every tool call's arguments and result. One example is an agent framework
named "Hermes." (This Hermes is unrelated to this repo's own
`BERSERK_LLM_HERMES_URL`/`HERMES_API_KEY` provider settings, described
above — those configure *berserk-mcp's own* upstream chat-completions
client for generation, not an MCP host.)

`scripts/hermes_tool_call_log.py` walks that transcript store. It emits one
full-fidelity JSON line per tool call — model, arguments, result,
untruncated — filterable by MCP server name. Use it to confirm which model
actually drove a tool call, or pipe it into `jq` for ad-hoc auditing. MCP's
stdio transport does not expose the caller's model identity to the server,
so this script fills that gap without berserk-mcp needing to know it.

## Choosing a model

The fixed-query design's core idea is that **the model never writes KQL**.
It only picks a tool and a time window. This lowers the skill the model
needs: instead of "can author correct Kusto," it only needs "can do basic
tool-calling." That is what makes cheap and local models viable in
principle. But the real floor is higher than earlier guidance here
claimed. A real-model eval sweep (2026-08-22/23; 8 models, 2 local via
Ollama and 6 cloud via OpenRouter; full methodology and per-model table in
[docs/model-routing-cost-validation-2026-08-23.md](docs/model-routing-cost-validation-2026-08-23.md))
found:

- **7-8B local models are not viable against the full tool schema.**
  Qwen2.5:7b and Llama3.1:8b scored 5-7% tool-selection accuracy — well
  below a dumb keyword-matching baseline (66%). The previous recommendation
  here ("7B is the sweet spot") was wrong; corrected in v1.26.0.
- **The measured reliability floor is the ~24B parameter class.**
  `mistral-saba` (24B) reached 83% tool-selection accuracy. One size class
  down (`mistral-nemo`, ~12B) fell *below* the keyword baseline.
- **Best measured performer:** `deepseek-chat` (93% tool-selection, 98%
  argument accuracy). **Best cost/performance:** `deepseek-v4-flash` (88%
  tool-selection, ~38x cheaper per call than `deepseek-chat` thanks to real
  prompt caching — verified against actual billed `usage.cost`, not sticker
  price). Caveat: `tool_choice: "required"` silently disables that caching;
  use `"auto"` to keep it.

If a small local model is a hard requirement, use [just-in-time tool
discovery](#just-in-time-tool-discovery-find_tool-opt-in) — cutting the
schema from 69 tools to 8 is a real, measured accuracy lever (92% token
reduction), though it did not close the gap to zero for the 7-8B models in
this sweep. For unattended local deployments, prefer a ≥24B model with a
GPU over a smaller one.

- **Cheap API.** `deepseek-v4-flash`, `gpt-4.1-mini`, Claude **Haiku**, or
  Gemini **Flash** give strong tool use at a fraction of frontier cost.
  Good for latency-sensitive ChatOps replies.
- **Frontier models** are rarely necessary. Save them for open-ended
  investigations that lean on `search` and `save_query`, or as an
  escalation tier for cases a cheaper model's own routing confidence flags
  as uncertain (see `evals/escalation_policy.py`).

The biggest reliability lever, regardless of model, is the tool
**descriptions**. They are written to be narrow and unambiguous, so a small
model routes correctly. Keep new tool descriptions that way.

## Security

berserk-mcp applies defense in depth across the execution boundary, KQL
validation, secret/PII redaction, generation-pipeline resource bounds,
concurrency-safe store writes, role-visibility enforcement, and
outbound-HTTP hardening. Each control has a name and an adversarial
regression test. See [Security controls](docs/security-controls.md) for
the full list of about 30 controls, plus the audit history: a hand audit, a
differential re-review, and an external scanner pass across three tools.
One open finding as of 2026-08-29: the HTTP transport's DNS-rebinding
protection (`BERSERK_MCP_HTTP_ALLOWED_HOSTS`) is opt-in rather than
defaulted on for a loopback bind — see
[docs/mcp-conformance.md](docs/mcp-conformance.md) and
[issue #84](https://github.com/ssimonsen0202/berserk_mcp/issues/84).

The server has also been run against the official
[MCP conformance test suite](https://github.com/modelcontextprotocol/conformance) —
results, including that one finding, in
[docs/mcp-conformance.md](docs/mcp-conformance.md).

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Wrong-answer containment

berserk-mcp groups its controls against a *confident false negative* under
one name. A confident false negative is an agent reporting a clean bill of
health when a query silently matched zero rows, went stale, or the tool
refused to run a broken query. Most open-source observability MCP
implementations state hallucination defenses like rate limiting, query
timeouts, and read-only execution. These protect backend stability. Few
address this query-result failure mode — the one that actually pages
someone at 4am.

Six controls make this up, each with a locking test: field-access guidance
for nested OTLP attributes, full-text search term-boundary guidance, KQL
validation that rejects blockers before execution, schema-drift warnings on
saved queries, a result envelope that tells apart the bare `(no rows)`
sentinel, and untrusted-data fencing against a smuggled instruction in a
log line. See [docs/wrong-answer-containment.md](docs/wrong-answer-containment.md)
for full detail, known limits, and the regression test for each.

## Testing

```bash
python -m pytest tests/ -q
# stdlib unittest is also supported:
python3 -m unittest discover -s tests
```

The tests stub the `bzrk` CLI. They verify: KQL content and lock strings,
default time windows, role isolation (which tools appear in which lane),
injection guards, `since` validation, tool annotations, JSON-RPC protocol,
the learning loop, discovery-queue deduplication, and amendments-log
behavior. The parser-factory suite additionally fakes the LLM HTTP layer, to
verify the escalation ladder, source profiling, new-source/drift detection,
generation, validation, refinement, and headless worker mode. The
agent-analytics suite verifies loop detection, model-fit classification, MCP
dispatch, and the headless `--agent-report` path.

### Live-verified, not just unit-tested

The stubbed suite proves berserk-mcp's logic is internally consistent. It
does not prove the KQL executes correctly against a real cluster. So every
SRE and SOC tool also runs through berserk-mcp's real dispatch path against
a live Berserk deployment, as part of the release process. This live pass
confirms, among other things:

- `soc_new_services` uses a `24h ago` default window with a shard-field filter, returning full results in about 28 seconds against real data volume.
- `sre_host_headroom` reports memory in GB, with an explicit `unit` column distinguishing it from the CPU load-average rows — matching `host_memory`'s units.
- The `trace_*` tools (v1.14.0) sort correctly: `trace_find_slow` casts `duration` to an integer before sorting, and `trace_analyze` orders spans by `start_time`. See [Trace tools](#trace-tools-all-lanes) above.
- `claude_token_burn`, `claude_loop_check`, and `claude_model_fit` (v1.14.1) parse real `bzrk --json` output correctly, including its `Tables[0].rows`/`Tables[0].schema.columns` shape. See [Agent-log intelligence](#agent-log-intelligence) above.

## Extending — add a new tool in five minutes

berserk-mcp's core idea is fixed, verified queries. Adding a tool is a
short, mechanical task. Keep the routing surface small (about 20 core
tools). Let less common tools accumulate behind `save_query`/`run_saved`
through the learning loop.

Before writing KQL, read the [Berserk KQL performance guide](docs/kql-performance-guide.md).
It covers index-friendly predicates, `tail` for recency, narrow projections,
explicit limits, live verification, and the shared-cluster fleet rules. As of
v1.17.0, the guide's "Verified function availability" table also confirms
`make-series`, `series_fit_line`, `series_decompose_anomalies`, `series_fir`,
`rate`, `deriv`, `bin_auto`, `extract_log_template`, and `fieldstats` all work
against the live cluster — every core query builder now prefers these native
forms over hand-rolled `bin()`/`sort`/`bag_keys` equivalents where one exists.

**1. Find the KQL on a live instance.** Iterate with `bzrk` until the query
returns clean rows — names, units, sort order. *Do not ship a query you have
not seen succeed against real data.*

```bash
bzrk -P local search "default | where metric_name == 'system.network.io' \
  | summarize bytes=sum(value) by host=tostring(resource['host.name'])" \
  --since "1h ago"
```

**2. Add the tool entry:**

```python
TOOLS.append({
    "name": "host_network",
    "roles": ["sre"],          # omit to make visible to all lanes
    "description": "Total network bytes (sum) per host. Per-HOST; for per-container "
                   "network use `search` for now.",
    "inputSchema": {"type": "object", "properties": _since()},
})
TITLES["host_network"] = "Per-Host Network I/O"
```

Wire it to the dispatcher (fixed `cmd` key), and add a KQL constant for the
test.

**3. Lock the query string with a test:**

```python
def test_q_host_net_locked(self):
    self.assertIn("system.network.io", bm.Q_HOST_NET)
```

**4. Run the suite and re-register:**

```bash
python -m pytest tests/ -q
claude mcp remove berserk-q && claude mcp add berserk-q -- berserk-mcp
```

A tool that touches free-text input (a service name) needs an allowlist —
see `logs_for_service`. A tool that needs two `bzrk` round-trips can follow
`discover_schema`'s pattern. Both patterns are in the source, as templates.

## Contributing

Issues, ideas, and PRs are all welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the short version. The bar is low: if the tests pass, the description is
narrow, and the query has been seen working against real data, it is
mergeable.

Good first contributions:

- A new fixed-query tool for telemetry you actually care about
- A worked example for your stack (Kubernetes, ECS, Nomad, and others) under [docs/](docs/)
- Sharpening a tool description that confused your model. The descriptions are the router — a clearer one is a real correctness improvement.
- Filing an issue when you hit something berserk-mcp should have a tool for

## License

[MIT](LICENSE).

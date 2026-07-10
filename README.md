# berserk-mcp

[![CI](https://github.com/ssimonsen0202/berserk_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ssimonsen0202/berserk_mcp/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server that lets an LLM answer
[Berserk](https://bzrk.dev) observability questions by **calling tools**
instead of hand-authoring KQL.

> **Why this matters:** when you hand a model a raw query language, it guesses —
> wrong table names, wrong field names, subtly broken aggregations — and you pay
> for the retries. Every tool here wraps a *verified* Kusto/KQL query, so the
> model picks an intent (`top_cpu`, `errors_by_service`, `sre_host_headroom`) and
> the query is fixed. Determinism is the whole point. In practice this makes
> even small/cheap models answer observability questions reliably.

- **Zero dependencies.** Pure Python standard library — nothing to `pip` beyond the package itself.
- **Single file.** `berserk_mcp.py` is the entire server. Easy to read, audit, and vendor.
- **Cross-platform.** Runs anywhere the `bzrk` CLI is installed, Windows included.
- **Safe by construction.** Fixed queries, input validation on free-text tools, no `shell=True`, and the Berserk token never touches this code.

> ## ⚠️ Disclaimer — please read
>
> This is an **unofficial, community-built** project. It is **not affiliated with,
> sponsored by, endorsed by, or supported by the Berserk project or its maintainers.**
> It interacts with Berserk only through the public `bzrk` CLI — no internal APIs, no
> reverse engineering.
>
> Provided **as-is, with no warranty and no liability** for any use, outcome, downtime,
> data loss, cost incurred, or other consequence (see [LICENSE](LICENSE)). You run it
> at your own risk against your own infrastructure. If you point it at a production
> Berserk, that's your call.
>
> Bugs, feature requests, and questions about *this* server: open an issue here.
> Anything about Berserk itself goes to the Berserk project — not us.

## Why this exists

**Berserk** is a self-hosted observability backend: it ingests logs, metrics, and
traces over OTLP and lets you query them with a Kusto-style language (KQL) through
the `bzrk` CLI or its web UI. It's the storage and the query engine, and it assumes
a human who already knows KQL.

**The gap.** A raw query language is the one thing LLMs are reliably bad at. Point a
model at `bzrk` and it invents table names, mistypes fields, and burns tokens on
retries. The obvious fixes — pasting the schema into the prompt, few-shot KQL
examples — were tried first and *didn't hold*: the model kept guessing. Hardcoding
the queries did.

**What berserk-mcp adds.** It's a translation layer in front of Berserk that
exposes observability *intents* as MCP tools (`top_cpu`, `errors_by_service`,
`sre_service_health`, …). Each wraps a query already verified against the live schema,
so the model never authors KQL — it picks an intent and a time window. It does **not**
replace Berserk's storage, query engine, or UI; it makes them **agent-accessible and
reliable on small / cheap / local models**.

Beyond the fixed tools, the server adds three layers that don't exist in default Berserk:

1. **Role lanes** — tool visibility filtered by job function so each agent sees only what it needs
2. **Discovery queue + auto-KQL worker** — automated onboarding of new telemetry sources
3. **Amendments log** — every `save_query` write is tracked so a worker can post changelogs and keep the query store auditable

| Approach | Result |
|---|---|
| Berserk web UI / `bzrk` CLI | Great for a human who knows KQL; not usable by an agent. |
| Point an LLM at the raw CLI + schema docs | Unreliable — models guess table/field names and pay for retries. |
| A generic "text-to-KQL" MCP | Still *authors* queries → same guessing problem, one layer up. |
| **berserk-mcp** | Fixed, verified queries → deterministic answers, even from a 7B local model. |

### What this adds vs. default Berserk

Berserk is a great human-facing observability backend on its own. This server doesn't
replace any of it — it sits next to it and adds the agent-facing surface. Concretely:

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
| **Custom-query persistence** as named, reusable tools | UI has a Query Library, but Berserk documents no API or CLI verb to create, list, or share a saved query programmatically | ✅ `save_query` (verify-before-persist) → `run_saved`, agent-readable |
| **Automated source onboarding** | — | ✅ `request_discovery` → worker → saved query, no KQL authoring needed |
| **Query changelog / amendments log** | — | ✅ every `save_query` write tracked; worker posts Discord diff |
| **Two-lane cost model** (cheap default · on-demand `@deep`) | — | ✅ tool descriptions + annotations make this safe |
| **KQL-injection guards** on free-text inputs | n/a (humans) | ✅ service-name allowlist · `claude_search` reject-list |

---

## Architecture

### How the lanes talk to each other and to Berserk

```mermaid
flowchart TB
  classDef user      fill:#0d1117,stroke:#58a6ff,color:#c9d1d9
  classDef cheap     fill:#0d3a1d,stroke:#3fb950,color:#c9d1d9
  classDef deep      fill:#3a1d0d,stroke:#d29922,color:#c9d1d9
  classDef mcp       fill:#161b22,stroke:#8b949e,color:#c9d1d9
  classDef berserk   fill:#1d1d3a,stroke:#a371f7,color:#c9d1d9
  classDef store     fill:#0d1117,stroke:#8b949e,color:#c9d1d9,stroke-dasharray:3 3

  User([User · Slack bot · agent framework]):::user

  subgraph H["MCP Host  (Claude Code · Claude Desktop · LangChain · ChatOps bot)"]
    direction TB
    Cheap["⚡ DEFAULT lane\ncheap / local model\ngpt-4.1-mini · Qwen2.5-7B · Haiku\nonly picks tools + time windows"]:::cheap
    Deep["🧠 @deep / scheduled lane\ncapable model\nsonnet · GPT-class\nauthors + verifies KQL"]:::deep
  end

  subgraph M["berserk-mcp  (stdio · JSON-RPC 2.0 · zero-dep stdlib Python)"]
    direction TB
    RoleFilter["Role filter  BERSERK_MCP_ROLE=sre|soc|claude|ops|all\ntools/list filtered at protocol level\nprimer injected at initialize"]:::mcp
    Tools["Fixed tools — verified KQL\ntop_cpu · errors_by_service · host_cpu\nlogs_for_service · sre_* · soc_* · claude_*"]:::mcp
    Disc["Discovery tools\nlist_metrics · discover_schema\ncontainer_hosts · list_services · schema"]:::mcp
    Learn["Learning loop\nsearch → save_query → run_saved\nverify-before-persist · amendments log · 500 cap"]:::mcp
    Queue[("discovery_queue.json\nknown_sources.json\namendments_log.json")]:::store
    Store[("learned.json\n~/.config/berserk-mcp")]:::store
  end

  Worker["discover-worker\ndrains queue · authors KQL · posts Discord\nruns via daily cron"]:::deep
  Bzrk["bzrk CLI\nbearer token lives only in bzrk's own 0600 config\nMCP never reads or stores it"]:::berserk

  subgraph B["Your Berserk instance"]
    direction TB
    Gw["Berserk gateway · KQL engine"]:::berserk
    Tbl[("default table\nOTLP logs · metrics · traces")]:::berserk
  end

  User -- "natural-language Q" --> Cheap
  User -- "@deep prompt · once-a-day cron" --> Deep

  Cheap -- "tools/call — role-filtered tools" --> RoleFilter
  RoleFilter --> Tools
  RoleFilter --> Disc
  RoleFilter --> Learn

  Deep -- "discover → search KQL → save_query" --> Learn
  Deep -- "request_discovery" --> Queue

  Queue --> Worker
  Worker -- "save_query per source" --> Learn
  Worker -- "Discord summary" --> User

  Tools -. "argv list (no shell)" .-> Bzrk
  Disc  -. "argv list (no shell)" .-> Bzrk
  Learn -. "verifies query before persist" .-> Bzrk
  Learn <-->|persist · reuse| Store

  Bzrk -- "read-only KQL over bearer auth" --> Gw
  Gw --> Tbl

  Learn -. "saved queries reusable by Cheap forever" .-> Cheap
```

Three things the diagram makes clear:

1. **The bearer token never enters this code.** `bzrk` owns it in its own 0600 config; the MCP shells out via an argv list (no shell, no token in process memory, no logs).
2. **The learning loop closes back into the cheap lane.** Pay the capable model once to author + verify a query; the cheap lane runs it free forever via `run_saved`.
3. **The worker is the automation bridge.** When `request_discovery` queues a new source, the worker drains it autonomously — discovers, authors KQL, saves — without operator KQL authoring.

---

## Role lanes

Set `BERSERK_MCP_ROLE` to scope what an agent sees. The filter applies at the MCP
protocol level — unrelated tools never appear in `tools/list`, so they can't be called
accidentally or injected into context.

| Role | `BERSERK_MCP_ROLE` | Gets | Typical agent |
|---|---|---|---|
| SRE | `sre` | Core tools + SRE tools (error rate, host headroom, ingest health, service health, top errors) | On-call Slack bot, editor assistant |
| SOC | `soc` | Core tools + SOC tools (high-severity logs, log spike, new services, repeated errors, incident timeline) | Security monitoring agent |
| Claude Code | `claude` | Core tools + Claude Code telemetry tools (sessions, tool histogram, errors, full-text search) | Developer workflow assistant |
| Ops | `ops` | All tools (full visibility) | Operator shell, admin scripts |
| Default | `all` (or unset) | All tools | Development, evaluation |

### Role primers

When a lane connects, the server injects a markdown primer into the MCP `initialize`
response before the standard instructions. Primers carry:

- **Tool routing table** — which tool to reach for first for each intent
- **Escalation thresholds** — e.g. CPU load > 2.0, mem > 85%, error rate > 10/min, ingest lag > 30 s
- **KQL authoring rules** — time window defaults, field name conventions, aggregation patterns
- **Discovery flow guidance** — when to call `request_discovery` vs authoring ad-hoc KQL

This means no prompt engineering is needed in the agent config; the routing knowledge
travels with the server.

Primers live in `primers/<role>.md` adjacent to the server file (or at
`BERSERK_MCP_PRIMERS_DIR` if set). The `all` / `ops` roles receive no primer — they're
expected to route from the tool descriptions directly.

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
| `list_hosts` | All hosts reporting telemetry (HermesRuntime, OpenClaw, ESXi, …). |
| `host_cpu` | Per-**host** CPU (1-minute load average). Default for ambiguous whole-machine CPU questions. |
| `host_memory` | Per-**host** memory used (GB). Default for ambiguous whole-machine memory questions. |
| `container_hosts` | Which host/VM each container runs on (join key for container↔host questions). |
| `logs_for_service` | Recent log lines for one service. |
| `schema` | Live tables + column schema introspection. |
| `list_metrics` | Every metric name being ingested, with counts (discovery). |
| `discover_schema` | Sample rows to learn an unknown source's `resource`/`attributes` shape. |
| `bzrk_query_perf` | Berserk query engine latency percentiles (p50/p95/p99 in µs). |
| `search` | Run arbitrary KQL (escape hatch; `save_query` the result once it works). |

Every query tool takes an optional `since` argument (`"15m ago"`, `"1h ago"`,
`"2d ago"`, …) with a sensible per-tool default.

**Per-host vs per-container:** `host_cpu`/`host_memory` report per **host**; `top_cpu`/`top_memory` report per **container**. The descriptions cross-reference each other so the model picks the right one. For ambiguous whole-machine questions ("what's hammering the server?") always prefer the host tools.

### SRE tools (`sre` lane only)

| Tool | What it answers |
|---|---|
| `sre_error_rate` | Error log events by service grouped per minute — "is the error rate climbing?" |
| `sre_host_headroom` | CPU load and memory by host — "which VM is saturated?" |
| `sre_ingest_health` | Berserk ingest lag and dropped data — "is observability lagging?" |
| `sre_service_health` | Full health summary for one named service: event volume, error count, log/metric split, last seen. |
| `sre_top_error_messages` | Most-repeated error messages by service — "what error should I investigate first?" |

### SOC tools (`soc` lane only)

| Tool | What it answers |
|---|---|
| `soc_high_severity_logs` | Recent CRITICAL/FATAL log lines with service and message text. |
| `soc_log_spike` | Services with the largest minute-level log bursts — "anything spiking?" |
| `soc_new_services` | Recently first-seen services and sources — "what is new?" |
| `soc_repeated_errors` | Error messages that repeat persistently — probes, loops, stuck processes. |
| `soc_timeline` | Full incident timeline for one named service: timestamps, severity, metric names, message snippets. |

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

### Learning loop tools (all lanes)

| Tool | What it answers / does |
|---|---|
| `list_saved` | List saved queries visible to the current role. Check here before authoring new KQL. |
| `run_saved` | Run a saved query by name — deterministic, no KQL authoring. |
| `save_query` | Verify a KQL query runs, then persist it under a name (with optional role tag). Logs every write to the amendments log. |

### Discovery tools (all lanes)

| Tool | What it does |
|---|---|
| `request_discovery` | Queue a newly-added service or metric for automated onboarding. Validates the source exists in Berserk before accepting. |
| `discovery_status` | List pending and completed discovery jobs. |

---

## Self-extending: discovery + learning

The fixed tools cover known telemetry. For data the server doesn't have a tool for yet —
a log source you just started shipping — a two-stage loop extends the server without
hand-editing code, while keeping the cheap lane deterministic.

### Stage 1: Discovery queue

```
QUEUE    request_discovery(service="haproxy")   →  validates source, queues job
WORKER   discover-worker drains queue at 06:00  →  authors KQL by role/kind
SAVE     save_query (verify-before-persist)      →  permanent, named query
REUSE    run_saved("sre_haproxy_service")        →  cheap model, free, forever
```

`request_discovery` does one thing before accepting a job: it calls `list_services`
(or `list_metrics`) to confirm the source is actually visible in Berserk. An unknown
source is rejected with a clear error, so the queue never fills with phantom jobs.

The **discover-worker** (`discover-worker.py`, runs as a daily cron) drains the queue:

- Chooses the right KQL template per role (`sre` gets a health summary, `soc` gets an incident timeline, `claude` gets a health rollup, `metric` kind gets a drilldown aggregation)
- Calls `save_query` to verify and persist the result
- Updates `known_sources.json` so the same source is never re-queued
- Posts a Discord summary of completed and failed jobs

### Stage 2: @deep amendments and improvements

When a capable model (`@deep`, a scheduled agent, or an operator) improves or corrects
an existing query via `save_query`, the server:

1. Detects whether the query name already existed (`action=updated`) or is new (`action=created`)
2. Appends a timestamped entry to `amendments_log.json` with the name, description, KQL preview, role, and action
3. The worker reads and clears this log on the next drain run, posting a Discord changelog: `✏️` for updates, `✨` for new entries

This means **the query store is auditable** — every improvement made by an autonomous
agent is surfaced in the team channel automatically, without any operator action.

The intended division of labour (cost-efficient):

- **A capable model does the rare, hard part** — discover the new shape, author + verify the query, `save_query` it. Trigger it two ways: on a **schedule** (a daily job that checks the discovery queue), or **on demand** ("I just added HAProxy to Berserk — add support").
- **The cheap model reaps the result** — every saved query is reusable for free via `run_saved`, deterministically. Authoring KQL is the one thing small models are weak at, so gate this behind the stronger model; `save_query` verifies the query runs before persisting as a guardrail.

This scales because **learned queries live behind `list_saved`/`run_saved`**, not as
first-class tools — so you can learn dozens of new sources without growing the routing
surface that keeps the cheap model reliable.

---

## Parser factory: LLM-generated query packs

Modeled on Microsoft's [ASIM parser AI agent for Sentinel](https://learn.microsoft.com/en-gb/azure/sentinel/normalization-create-parsers-ai-agent):
sample the source → generate KQL → validate by executing it → refine on
failure (capped at 5 cycles) → persist the survivors. Where Sentinel's agent
produces stored ASIM parser functions, Berserk has no stored functions, so
the output here is a **query pack**: 2–4 verified `save_query` entries per
source (an overview, an errors/timeline view, and metric aggregates where
appropriate).

**Escalation ladder.** Generation tries providers in order — free/local
first, expensive only on failure:

```
hermes (local/free) → openai → anthropic
```

Each provider gets up to 5 refinement attempts, with the previous failure's
validator error fed back into the next prompt. A provider that isn't
configured (no API key) is skipped after one attempt rather than burning the
full 5.

**Tools:**

| Tool | What it does |
|---|---|
| `detect_new_sources` | Scans Berserk for services/metrics never seen before, and optionally schema drift on known ones (new attribute keys on an existing service). `auto_queue=true` feeds newcomers into the discovery queue. |
| `generate_parser` | Synchronously generates and verifies a query pack for one named source right now. |
| `run_discovery_worker` | Drains up to N pending discovery jobs through the pipeline. |
| `review_generated` | Lists or inspects LLM-generated saved queries — audit before trusting them. |

**Safety.** Generated KQL passes through the exact same `_KQL_PREFIX_RE`
guard as human input, and is only saved if it executes successfully against
Berserk. A generated query never silently overwrites a human-saved one — on
a name collision it's saved as `<name>_gen` instead. Every generated entry
carries `generated_by: {provider, model, ts, job_source}` so `review_generated`
can audit it before anyone trusts it in production. See
[SECURITY.md](SECURITY.md) for the full threat model, including the
indirect-prompt-injection risk from log data fed into generation prompts.

**Runaway fail-safes.** Auto-discovery is deliberately bounded so it can never
flood the queue or burn a pile of LLM tokens in one pass — a real cluster can
have hundreds of metrics:

- **Internal metrics are never auto-queued.** `detect_new_sources` records them
  in the baseline (so they don't re-flag as "new") but only ever queues
  *services* — the assistant never needs a per-metric query pack.
- **Per-run service cap.** A single detect pass queues at most
  `MAX_AUTOQUEUE_PER_RUN` new services (default **5**, override via
  `BERSERK_MAX_AUTOQUEUE`); any remainder is picked up on later runs.
- **Per-run drain cap.** `run_discovery_worker` / `--worker` generate for at
  most a bounded number of jobs per invocation (`--max-jobs`, capped at 5), so
  a large pending queue drains gradually rather than all at once.
- **Ephemeral-name filter.** Service names with no letters (e.g. a bare PID or
  changing numeric id emitted as `service.name` by a misconfigured source) are
  skipped — otherwise they look "new" every run and would queue a junk pack
  forever.

The first `detect_new_sources` run against a fresh Berserk *seeds the baseline
and queues nothing* — everything looks new on day one, so it records the
current state as the "known" set rather than generating hundreds of packs.

**Headless / cron mode.** MCP stdio servers only run while a client is
attached, so there's a CLI path for unattended scheduling:

```bash
python3 berserk_mcp.py --worker --auto-queue --max-jobs 2 --check-drift
```

Detects new sources, queues them, drains up to `--max-jobs` pending jobs, and
exits 0 (or 1 if any job needed human review). Example cron line:

```
*/30 * * * * cd /path/to/berserk-mcp && python3 berserk_mcp.py --worker --auto-queue --max-jobs 2 >> ~/.local/state/berserk-worker.log 2>&1
```

**Configuration** (all optional — a provider with no key configured is
skipped):

| Variable | Default | Purpose |
|---|---|---|
| `BERSERK_LLM_LADDER` | `hermes,openai,anthropic` | Provider order for generation. |
| `HERMES_API_KEY` | — | Bearer token for the Hermes/Open WebUI endpoint. |
| `BERSERK_LLM_HERMES_URL` | — | Hermes chat-completions endpoint. |
| `BERSERK_LLM_HERMES_MODEL` | auto-discovered via `/api/models` | Hermes model id. |
| `OPENAI_API_KEY` | — | OpenAI API key. |
| `BERSERK_LLM_OPENAI_MODEL` | `gpt-4o` | OpenAI model. |
| `ANTHROPIC_API_KEY` | — | Anthropic API key. |
| `BERSERK_LLM_ANTHROPIC_MODEL` | `claude-opus-4-8` | Anthropic model. |
| `BERSERK_LLM_TIMEOUT` | `120` | Per-LLM-call timeout, seconds. |
| `BERSERK_MAX_AUTOQUEUE` | `5` | Max new services a single `detect_new_sources` pass will queue (runaway fail-safe). |

No new pip dependencies — LLM calls use `urllib.request` from the standard
library, matching the rest of the server's zero-dependency design.

---

## Worked examples

Concrete prompts you can paste into any MCP-aware client. Each shows the natural-language
ask, which tools the model ends up calling, and the kind of answer you get. These all
work on the cheap default lane — no frontier model required.

### ChatOps: "any errors in the last hour?" (SRE lane)

```
Have there been any errors in the last hour, and from which service?
```

> Calls `errors_by_service` (`since="1h ago"`). The model replies with the per-service
> error count, or "no errors recorded" when empty. On the SRE lane, the primer nudges
> it toward `sre_error_rate` for a time-series view if the count is above threshold.

### On-call triage: "is api-gateway healthy?" (SRE lane)

```
Is api-gateway healthy? What's the error rate and when was it last seen?
```

> Calls `sre_service_health(service="api-gateway")`. Returns total events, error count,
> log/metric split, and last-seen timestamp in one round trip. If error count is high,
> the primer's threshold guidance nudges the model to follow up with `sre_top_error_messages`.

### SOC investigation: "what happened on journal-forwarder?" (SOC lane)

```
Reconstruct what happened with journal-forwarder over the last 2 hours.
```

> Calls `soc_timeline(service="journal-forwarder", since="2h ago")`. Returns timestamped
> events with severity, metric names, and message snippets ordered newest-first —
> a ready-made incident narrative without any KQL authoring.

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

> Calls `claude_tools(since="7d ago")` + `claude_errors`. Only works if you ship Claude
> Code session logs into Berserk via an OTLP forwarder — see [docs/claude-code.md](docs/claude-code.md).

### Onboarding a new source

```
I just added HAProxy logs to Berserk. Integrate it.
```

> (With `SOUL.md` / system prompt configured.) Agent calls
> `request_discovery(service="haproxy", role_hint="sre")`. The discovery worker runs
> overnight, authors and saves `sre_haproxy_service`. Next morning `run_saved` answers
> HAProxy questions on the cheap lane, permanently.

### Autonomous daily health digest (cron / scheduled agent)

```
You are an on-call assistant. Use the Berserk MCP to:
1) Check load per host (host_cpu, host_memory) over the last 6 hours.
2) Count errors per service over the last 24 hours (errors_by_service).
3) List the top 5 noisiest containers (top_memory).
Write a 10-line digest, flag anything anomalous, and stop.
```

> Deterministic enough to run unattended overnight on `gpt-4.1-mini` or a local
> Qwen2.5-7B. Wire it to a cron job; the answer is short and parseable.

---

## Requirements

- Python 3.8+
- The [`bzrk`](https://docs.bzrk.dev) CLI, installed and authenticated (`bzrk -P <profile> search "..."` works). The bearer token lives in `bzrk`'s own config — this server never reads or stores it.

## Install

```bash
pip install berserk-mcp
# or, isolated:
pipx install berserk-mcp
# or run without installing:
uvx berserk-mcp
```

From source:

```bash
git clone https://github.com/ssi0202/berserk-mcp
cd berserk-mcp
pip install .
```

The single file has no dependencies, so you can also just drop `berserk_mcp.py`
somewhere and run `python berserk_mcp.py`.

## Configure

All configuration is via environment variables — all optional:

| Variable | Default | Purpose |
|---|---|---|
| `BZRK_BIN` | `bzrk` | Path/name of the Berserk CLI binary. |
| `BZRK_PROFILE` | `local` | The `bzrk` profile to query. |
| `BZRK_TIMEOUT` | `120` | Per-query timeout, seconds. |
| `BERSERK_TABLE` | `default` | The Berserk table to query. |
| `BERSERK_MCP_LEARNED_PATH` | platform config dir | Where saved queries persist (`~/.config/berserk-mcp/learned.json` on Linux). |
| `BERSERK_MCP_ROLE` | `all` | Active role lane: `sre`, `soc`, `claude`, `ops`, or `all`. Controls tool visibility and primer injection. |
| `BERSERK_MCP_PRIMERS_DIR` | adjacent `primers/` dir | Directory containing `<role>.md` primer files. |

Parser-factory (LLM parser generation) has its own env vars — see
[Parser factory](#parser-factory-llm-generated-query-packs) above.

## Connect it to a client

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

## Choosing a model

The whole point of the fixed-query design is that **the model never writes KQL** — it
only picks a tool and a time window. That collapses the capability bar from "can author
correct Kusto" down to "can do basic tool-calling," which is what makes cheap and local
models viable. Lead with the cheapest thing that works:

- **Local (preferred).** Any Ollama/LM-Studio model with solid tool-calling: the
  **Qwen2.5-Instruct** family (7B is the sweet spot), **Llama 3.1/3.3**, or
  **Mistral-Small**. Tiny models (≤2B) and CPU-only prefill struggle with agentic
  tool-call loops — prefer GPU and ≥7B for unattended use.
- **Cheap API.** `gpt-4.1-mini`, Claude **Haiku**, or Gemini **Flash** — strong tool
  use at a fraction of frontier cost. Good for latency-sensitive ChatOps replies.
- **Frontier models** are rarely necessary; save them for open-ended investigations
  that lean on `search` and `save_query`.

Biggest reliability lever regardless of model: the tool **descriptions**. They're
written to be narrow and unambiguous so a small model routes correctly — keep them
that way if you add tools.

## Security

- **Injection guards.** `logs_for_service` and `sre_service_health`/`soc_timeline` validate the service name against `[A-Za-z0-9._-]`. `claude_search` rejects quotes, pipe, backslash, and backtick. Both are interpolated into KQL string literals, so this blocks single-quote injection. All other tools use fully fixed queries with no interpolation.
- **Read-only by construction.** Every tool is annotated (`readOnlyHint`) and only issues read KQL. The sole exceptions are `save_query` (writes a local query file, never Berserk) and `request_discovery` (writes a local queue file).
- **No shell.** `subprocess` is always invoked with an argument list (never `shell=True`); there is no `eval`.
- **No secrets in this code.** The Berserk bearer token lives only in `bzrk`'s own config. This server never reads, stores, or logs it.
- **Note on output.** Tool results are whatever your telemetry contains. If logs in Berserk hold sensitive values, `logs_for_service`/`search` can surface them — redact at ingest, not here.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Testing

```bash
python -m pytest tests/ -q
# 111 tests total, including the new parser-factory pipeline (tests/test_parser_factory.py)
```

The tests stub the `bzrk` CLI, so they verify: KQL content and lock strings, default
time windows, role isolation (which tools appear in which lane), injection guards,
`since` validation, tool annotations, JSON-RPC protocol, learning loop, discovery queue
deduplication, and amendments log behaviour. The parser-factory suite additionally
fakes the LLM HTTP layer to verify the escalation ladder, source profiling, new-source/
drift detection, generation + validation + refinement, and headless worker mode.

### Live-verified, not just unit-tested

The stubbed suite proves the server's logic is internally consistent; it can't prove the
KQL actually executes correctly against a real cluster. Separately, every SRE and SOC
tool has been run through this server's real dispatch path against a live Berserk
deployment — and that process caught two real bugs unit tests alone couldn't surface:

- `soc_new_services`'s default 7-day window had no shard-field filter, so it scanned
  unindexed and timed out under real data volume. Narrowed to `24h ago`; confirmed
  returning full results in ~28s.
- `sre_host_headroom` returned raw bytes for memory instead of converting to GB (unlike
  `host_memory`, which already did) — summarized by a model as "1.61 billion bytes."
  Fixed: memory now reports in GB with an explicit `unit` column distinguishing it from
  the CPU load-average rows.

Both fixes are in the current release.

## Extending — add a new tool in five minutes

The whole point of this server is fixed, verified queries — so adding a tool is a
small, mechanical ritual. Aim to keep the routing surface small (~20 core tools) and
let the long tail accumulate behind `save_query`/`run_saved` via the learning loop.

**1. Find the KQL on a live instance.** Iterate with `bzrk` until the query returns
clean rows — names, units, sort order. *Don't ship a query you haven't seen succeed
against real data.*

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

Wire it to the dispatcher (fixed `cmd` key), and add a KQL constant for the test.

**3. Lock the query string with a test:**

```python
def test_q_host_net_locked(self):
    self.assertIn("system.network.io", bm.Q_HOST_NET)
```

**4. Run the suite + re-register:**

```bash
python -m pytest tests/ -q
claude mcp remove berserk-q && claude mcp add berserk-q -- berserk-mcp
```

A tool that touches free-text input (a service name) needs an allowlist (see
`logs_for_service`). A tool needing two `bzrk` round-trips can follow `discover_schema`'s
pattern. Both are in the source as templates.

## Contributing

Issues, ideas, and PRs are all welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for
the short version. The bar is low: if the tests pass, the description is narrow, and
the query has been seen working against real data, it's mergeable.

Good first contributions:

- A new fixed-query tool for telemetry you actually care about
- A worked example for your stack (Kubernetes, ECS, Nomad, …) under [docs/](docs/)
- Sharpening a tool description that confused your model — the descriptions are the router; a clearer one is a real correctness improvement
- Filing an issue when you hit something the server should have a tool for

## License

[MIT](LICENSE).

# Parser factory: LLM-generated query packs

**The problem it solves:** A new service or log type starts shipping to
Berserk, and there is no tool for it yet. Normally a human notices, explores
the shape with `discover_schema`, hand-writes KQL, and calls `save_query`.
The parser factory automates that loop.

The design follows Microsoft's [ASIM parser AI agent for Sentinel](https://learn.microsoft.com/en-gb/azure/sentinel/normalization-create-parsers-ai-agent):
sample the source, generate KQL, validate by executing it, refine on failure
(capped at 5 cycles), then persist the survivors. Sentinel's agent produces
stored ASIM parser functions. Berserk has no stored functions, so the output
here is a **query pack**: 2–4 verified `save_query` entries per source (an
overview, an errors/timeline view, and metric aggregates where appropriate).
Each entry is reusable forever afterward via `run_saved` on the cheap lane.

How the pipeline maps to Sentinel's ASIM agent stages:

| ASIM parser AI agent (Sentinel) | berserk-mcp parser factory |
|---|---|
| Requirements gathering | Discovery job — source name, kind, role hint |
| Sample source data (`getschema` + up to 2,000 rows) | `build_source_profile`: resource keys + row sample + `getschema` |
| Generate the KQL parser | LLM generates a JSON **query pack** from the profile |
| Schema validation (`ASimSchemaTester`) | Declared output columns checked against real query output |
| Data validation (`ASimDataTester`) | Query is **executed**; must return rows (window widened once before failing) |
| Refinement loop (≤ 5 cycles) | Validator error fed back to the model, ≤ 5 attempts per provider |
| Deploy / package | Persisted through the existing `save_query` store (which re-verifies) |
| Summary report | Report stored on the discovery job; visible via `discovery_status` / `review_generated` |

**Escalation ladder.** Generation tries providers in order: free and local
first, expensive only on failure.

```
hermes (local/free) → openai → anthropic
```

Each provider gets up to 5 refinement attempts. The previous failure's
validator error feeds back into the next prompt. A provider with no
configuration (no API key) is skipped after one attempt, instead of burning
the full 5.

**Tools:**

| Tool | What it does |
|---|---|
| `detect_new_sources` | Scans Berserk for services and metrics never seen before, and optionally for schema drift on known ones (new attribute keys on an existing service). `auto_queue=true` feeds newcomers into the discovery queue. |
| `generate_parser` | Synchronously generates and verifies a query pack for one named source, right now. |
| `run_discovery_worker` | Drains up to N pending discovery jobs through the pipeline. |
| `review_generated` | Lists or inspects LLM-generated saved queries. Audit these before you trust them. |

**What it produces.** For a newly-detected `haproxy` service, one run turns
this discovery job:

```
generate_parser(service="haproxy", role_hint="sre")
```

into a set of verified, source-prefixed saved queries. Only the queries that
actually returned rows are kept:

```
haproxy_overview            – event volume, log/metric split, last seen
haproxy_error_rate          – ERROR lines per minute
haproxy_top_backends        – requests grouped by backend
```

Each entry is stored with `generated_by: {provider, model, ts, job_source}`.
Each is immediately runnable on the cheap lane via
`run_saved name=haproxy_overview`.

**Safety.** Generated KQL passes through the same `_KQL_PREFIX_RE` guard as
human input. berserk-mcp saves a generated query only if it executes
successfully against Berserk. A generated query never silently overwrites a
human-saved one; on a name collision, it saves as `<name>_gen` instead. Every
generated entry carries `generated_by: {provider, model, ts, job_source}`,
so `review_generated` can audit it before anyone trusts it in production. See
[SECURITY.md](../SECURITY.md) for the full threat model, including the
indirect-prompt-injection risk from log data fed into generation prompts.

**Runaway fail-safes.** Auto-discovery is deliberately bounded. It can never
flood the queue or burn a pile of LLM tokens in one pass — a real cluster can
have hundreds of metrics:

- **Internal metrics are never auto-queued.** `detect_new_sources` records
  them in the baseline, so they do not re-flag as "new." It only ever queues
  *services* — the assistant never needs a per-metric query pack.
- **Per-run service cap.** A single detect pass queues at most
  `MAX_AUTOQUEUE_PER_RUN` new services (default **5**; override with
  `BERSERK_MAX_AUTOQUEUE`). Any remainder is picked up on later runs.
- **Per-run drain cap.** `run_discovery_worker` and `--worker` generate for
  at most a bounded number of jobs per invocation (`--max-jobs`, capped at
  5). A large pending queue drains gradually, not all at once.
- **Ephemeral-name filter.** berserk-mcp skips service names with no letters
  — for example a bare PID, or a changing numeric ID emitted as
  `service.name` by a misconfigured source. Otherwise these names look "new"
  on every run and would queue a junk pack forever.

The first `detect_new_sources` run against a fresh Berserk *seeds the
baseline and queues nothing*. Everything looks new on day one, so this first
run records the current state as the "known" set, instead of generating
hundreds of packs.

**Headless / cron mode.** An MCP stdio server runs only while a client is
attached. A separate CLI path handles unattended scheduling:

```bash
python3 berserk_mcp.py --worker --auto-queue --max-jobs 2 --check-drift
```

This command detects new sources, queues them, drains up to `--max-jobs`
pending jobs, and exits 0 (or 1 if any job needed human review). Example cron
line:

```
* * * * * cd /path/to/berserk-mcp && python3 berserk_mcp.py --worker --auto-queue --max-jobs 2 >> ~/.local/state/berserk-worker.log 2>&1
```

The worker applies up to `BERSERK_WORKER_JITTER_SECONDS` of random startup
jitter, so the cron entry can run every minute without synchronizing many
tenants on a fixed minute.

**Configuration** (all optional; a provider with no key configured is
skipped):

| Variable | Default | Purpose |
|---|---|---|
| `BERSERK_LLM_LADDER` | `hermes,openai,anthropic` | Provider order for generation. |
| `HERMES_API_KEY` | — | Bearer token for the Hermes/Open WebUI endpoint. |
| `BERSERK_LLM_HERMES_URL` | `http://localhost:3000/api/chat/completions` | Hermes chat-completions endpoint. Resolution order: this env var, then a local `llm_config.json`, then the default. Persist a private URL without an env var, and without hardcoding it in the repo, using `berserk-mcp --set-hermes-url <URL>` — this writes `llm_config.json` in the per-user berserk-mcp config directory with current-user-only protection (`0600` on POSIX, a protected DACL on Windows). By default, plaintext `http://` is accepted only for a loopback host (`localhost`/`127.0.0.1`/`::1`). See `BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE` below if Hermes runs on a private or VPN network. |
| `BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE` | unset | Set to `1` to allow `BERSERK_LLM_HERMES_URL`/`--set-hermes-url` to point at a **non-loopback** host over plain `http://` — for example a Tailscale or private-LAN Hermes gateway. Without this setting, a non-loopback `http://` URL is rejected at both save-time and call-time, because the bearer token would otherwise cross the network unencrypted. Prefer `https://` when the endpoint supports it. Use this flag only for trusted private networks that do not support `https://`. |
| `BERSERK_LLM_HERMES_MODEL` | auto-discovered via `/api/models` | Hermes model id. |
| `OPENAI_API_KEY` | — | OpenAI API key. |
| `BERSERK_LLM_OPENAI_MODEL` | `gpt-4o` | OpenAI model. |
| `ANTHROPIC_API_KEY` | — | Anthropic API key. |
| `BERSERK_LLM_ANTHROPIC_MODEL` | `claude-opus-4-8` | Anthropic model. |
| `BERSERK_LLM_TIMEOUT` | `120` | Per-LLM-call timeout, seconds. |
| `BERSERK_MAX_AUTOQUEUE` | `5` | Max new services a single `detect_new_sources` pass will queue (runaway fail-safe). |

No new pip dependencies. LLM calls use `urllib.request` from the standard
library, matching the rest of berserk-mcp's zero-dependency design.

> **Note for Berserk maintainers.** This feature exists because Berserk has
> no stored-function or saved-view primitive that an agent can create
> programmatically. So "a parser for a source" is emulated as a bundle of
> verified saved queries in berserk-mcp's own store. If Berserk ever exposes
> a gateway RPC for stored KQL functions or server-side saved views (the ASIM
> parser equivalent), this pipeline could target that directly instead. The
> generated packs would then become first-class Berserk objects. Feedback on
> whether such a primitive exists or is planned is very welcome — see
> [CONTRIBUTING.md](../CONTRIBUTING.md).

# berserk-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an LLM answer
[Berserk](https://berserk.dev) observability questions by **calling tools**
instead of hand-authoring KQL.

> **Why this matters:** when you hand a model a raw query language, it guesses —
> wrong table names, wrong field names, subtly broken aggregations — and you pay
> for the retries. Every tool here wraps a *verified* Kusto/KQL query, so the
> model picks an intent (`top_cpu`, `errors_by_service`, `claude_sessions`) and
> the query is fixed. Determinism is the whole point. In practice this makes
> even small/cheap models answer observability questions reliably.

- **Zero dependencies.** Pure Python standard library — nothing to `pip` beyond the package itself.
- **Single file.** `berserk_mcp.py` is the entire server. Easy to read, audit, and vendor.
- **Cross-platform.** Runs anywhere the `bzrk` CLI is installed, Windows included.
- **Safe by construction.** Fixed queries, input validation on the two free-text tools, no `shell=True`, and the Berserk token never touches this code.

> **Unofficial integration.** This is a community-maintained project. It is not
> affiliated with, sponsored by, or endorsed by the Berserk project. It talks to
> Berserk only through the official `bzrk` CLI.

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

**What berserk-mcp adds.** It's a thin translation layer in front of Berserk that
exposes observability *intents* as MCP tools (`top_cpu`, `errors_by_service`,
`logs_for_service`, …). Each wraps a query already verified against the live schema,
so the model never authors KQL — it picks an intent and a time window. It does **not**
replace Berserk's storage, query engine, or UI; it makes them **agent-accessible and
reliable on small / cheap / local models**. A `search` escape hatch still allows
arbitrary KQL when you need it, and a learning loop (`save_query` → `run_saved`) turns
a one-off query into a permanent, named tool.

| Approach | Result |
|---|---|
| Berserk web UI / `bzrk` CLI | Great for a human who knows KQL; not usable by an agent. |
| Point an LLM at the raw CLI + schema docs | Unreliable — models guess table/field names and pay for retries. |
| A generic "text-to-KQL" MCP | Still *authors* queries → same guessing problem, one layer up. |
| **berserk-mcp** | Fixed, verified queries → deterministic answers, even from a 7B local model. |

### Use cases it supports or enhances

- **ChatOps.** A Slack / Discord / Teams bot answering "any errors in the last hour?"
  or "which container is eating CPU?" in plain language, backed by *your own* Berserk —
  no third-party SaaS, no telemetry leaving your network.
- **Autonomous monitoring agents.** A scheduled agent calls the tools to write a daily
  health digest, flag anomalies, or open a ticket — deterministic enough to run
  unattended on a cheap or local model.
- **On-call triage from your editor.** Ask about production from inside Claude Code or
  Claude Desktop without switching to a dashboard.
- **Any telemetry source.** Tools query generically by service / host / metric, so
  whatever feeds OTLP into Berserk — containers, VMs, Kubernetes, application code,
  edge devices — is queryable the same way, with no per-source configuration.
- **Claude Code observability (bonus).** If you ship Claude Code session logs into
  Berserk, five extra tools turn that into a queryable record of your own agent
  activity — sessions, tool histograms, errors, full-text search — which Berserk has
  no opinion about out of the box. See [docs/claude-code.md](docs/claude-code.md).

## Requirements

- Python 3.8+
- The [`bzrk`](https://berserk.dev) CLI, installed and authenticated to your Berserk instance (a working profile that `bzrk -P <profile> search "..."` can use). The bearer token lives in `bzrk`'s own config — this server never reads or stores it.

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
| `BERSERK_MCP_LEARNED_PATH` | per-user config dir | Where saved queries persist (see [Learning loop](#learning-loop)). |

## Connect it to a client

### Claude Desktop

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "berserk-q": {
      "command": "berserk-mcp",
      "env": { "BZRK_PROFILE": "local" }
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
      "env": { "BZRK_PROFILE": "local" }
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

### Any MCP client

Launch `berserk-mcp` (or `python berserk_mcp.py`) as a stdio MCP server. It speaks
newline-delimited JSON-RPC 2.0 over stdio.

## Using it with agents and bots

This is a standard **stdio MCP server**, so anything that can host an MCP server can
drive it — the host (your agent framework, Slack/Discord bot, or chat app) is the MCP
*client*; it spawns `berserk-mcp` as a subprocess and calls the tools.

- **Agent frameworks** (LangChain/LangGraph, the OpenAI/Anthropic Agents SDKs,
  smolagents, PydanticAI, etc.) all have an MCP-stdio adapter. Point it at the
  `berserk-mcp` command; the 19 tools appear automatically, with their `title`,
  `description`, and `annotations`.
- **Slack / Discord / Teams bots.** Run the bot as the MCP host on the same machine
  (or container) as a configured `bzrk`. The bot turns a message into a model turn,
  the model calls the tools, and the bot posts the answer back. Because the tools are
  read-only (see annotations), you can safely auto-approve them in the bot's policy.
- **Remote / shared deployment.** stdio is local-subprocess by design. To expose one
  server to several remote clients, run it behind an MCP stdio→HTTP bridge (e.g.
  `mcpo`/`supergateway`) and put auth + TLS in front — don't expose it raw. (A native
  Streamable-HTTP transport is on the roadmap; open an issue if you need it.)

The server holds no Berserk credentials of its own and only issues read-only KQL, so
the trust boundary is just "who can reach this process and what's in your telemetry."

## Choosing a model

The whole point of the fixed-query design is that **the model never writes KQL** — it
only picks a tool and a time window. That collapses the capability bar from "can author
correct Kusto" down to "can do basic tool-calling," which is exactly what makes cheap
and local models viable here. Lead with the cheapest thing that works:

- **Local (preferred).** Any Ollama/LM-Studio model with solid tool-calling: the
  **Qwen2.5-Instruct** family (7B is the sweet spot; 3B works for simple asks),
  **Llama 3.1/3.3**, or **Mistral-Small**. A 7B Q4 model fits in ~5–6 GB of VRAM and
  selects among these tools reliably because the names and `instructions` are
  unambiguous. Tiny models (≤2B) and CPU-only prefill struggle with the agentic
  tool-call loop — prefer a GPU and ≥7B for unattended use.
- **Cheap API (when local won't fit or you need speed).** `gpt-4.1-mini`, Claude
  **Haiku**, or Gemini **Flash** — all have strong tool use at a fraction of frontier
  cost. These are a good fit for latency-sensitive ChatOps replies.
- **Frontier models** are rarely necessary here; save them for open-ended
  investigations that lean on the `search` escape hatch.

Biggest reliability lever, regardless of model: the tool **descriptions**. They're
written to be narrow and unambiguous so a small model can route correctly — keep them
that way if you add tools.

## Tools

| Tool | What it answers |
|---|---|
| `list_containers` | Containers currently sending metrics (with sample counts). |
| `top_cpu` | Containers ranked by CPU %. |
| `top_memory` | Containers ranked by memory (MB). |
| `errors_by_service` | ERROR-level log counts grouped by service. |
| `list_services` | All services/sources, with log vs metric breakdown. |
| `list_hosts` | All hosts reporting telemetry, by record count. |
| `host_cpu` | Per-**host** CPU (1-minute load average). |
| `host_memory` | Per-**host** memory used (GB). |
| `logs_for_service` | Recent log lines for one service. |
| `schema` | Live tables + column schema introspection. |
| `search` | Run arbitrary KQL (escape hatch). |

Every query tool takes an optional `since` argument (`"15m ago"`, `"1h ago"`,
`"2d ago"`, …) with a sensible per-tool default.

**Per-host vs per-container:** `host_cpu`/`host_memory` report per **host**
(from host metrics); `top_cpu`/`top_memory` report per **container**. The tool
descriptions cross-reference each other so the model picks the right one.

### Claude Code telemetry tools

If you ship your Claude Code session logs into Berserk (service name
`claude-code`), five extra tools mine that data: `claude_recent`,
`claude_sessions`, `claude_tools`, `claude_errors`, and `claude_search`. See
[docs/claude-code.md](docs/claude-code.md) for the data shape and pipeline.

## Learning loop

When a question isn't covered by a standard tool, the model uses `search` to
answer it — then can persist the working query with `save_query`. Three tools
make this a one-time cost:

- `list_saved` — list saved queries (check here before authoring new KQL).
- `run_saved` — run a saved query by name (deterministic, no authoring).
- `save_query` — verify a query runs, then persist it under a name.

`save_query` runs the query once before persisting; a query that errors is **not**
saved. The store is capped at 500 entries. Saved queries live in
`BERSERK_MCP_LEARNED_PATH` (default: your platform config dir, e.g.
`~/.config/berserk-mcp/learned.json`).

## Security

- **Injection guards.** `logs_for_service` validates the service name against
  `[A-Za-z0-9._-]`, and `claude_search` rejects quotes, pipe, backslash, and
  backtick — both are interpolated into KQL string literals, so this blocks
  single-quote injection. The `since` window is validated against a strict time
  grammar. All other standard tools use fixed queries with no interpolation.
- **Read-only by construction.** Every tool is annotated (`readOnlyHint`) and only
  issues read KQL; the sole exception is `save_query`, which writes to a *local*
  query file (never to Berserk). Clients can use the annotations to auto-approve the
  read tools.
- **`search` / `save_query` accept arbitrary KQL by design** — but KQL is
  read-only; it cannot mutate data.
- **No shell.** `subprocess` is always invoked with an argument list (never
  `shell=True`); there is no `eval`.
- **No secrets in this code.** The Berserk bearer token lives only in `bzrk`'s
  own config. This server never reads, stores, or logs it.
- **Note on output.** Tool results are whatever your telemetry contains. If logs in
  Berserk hold sensitive values, `logs_for_service`/`search` can surface them —
  redact at ingest (e.g. in your OTLP forwarder), not here.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```bash
python tests/test_berserk_mcp.py     # 26 tests, no live Berserk required
```

The tests stub the `bzrk` CLI, so they verify the generated KQL, default time
windows, injection guards, `since` validation, tool annotations, the JSON-RPC
protocol, and the learning loop — all offline.

Adding a tool: add an entry to `SIMPLE` (for a fixed query) or handle it in
`handle_call`, add its metadata to `TOOLS`, and lock its KQL with a test. Keep
queries verified against a live instance before committing — determinism is the
point.

## License

[MIT](LICENSE).

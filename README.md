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
git clone https://github.com/USERNAME/berserk-mcp
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
  single-quote injection. All other standard tools use fixed queries with no
  interpolation.
- **`search` / `save_query` accept arbitrary KQL by design** — but KQL is
  read-only; it cannot mutate data.
- **No shell.** `subprocess` is always invoked with an argument list (never
  `shell=True`); there is no `eval`.
- **No secrets in this code.** The Berserk bearer token lives only in `bzrk`'s
  own config. This server never reads, stores, or logs it.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```bash
python tests/test_berserk_mcp.py     # 22 tests, no live Berserk required
```

The tests stub the `bzrk` CLI, so they verify the generated KQL, default time
windows, injection guards, JSON-RPC protocol, and the learning loop offline.

Adding a tool: add an entry to `SIMPLE` (for a fixed query) or handle it in
`handle_call`, add its metadata to `TOOLS`, and lock its KQL with a test. Keep
queries verified against a live instance before committing — determinism is the
point.

## License

[MIT](LICENSE).

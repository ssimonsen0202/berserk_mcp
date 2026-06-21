# Claude Code telemetry tools

`berserk-mcp` ships five tools that mine [Claude Code](https://claude.com/claude-code)
session activity, *if* you forward Claude Code's session logs into Berserk under
the service name `claude-code`. If you don't, ignore these — the other tools work
without them.

## Pipeline

```
Claude Code JSONL  (~/.claude/projects/*/*.jsonl)
  → a forwarder that tails the JSONL, redacts secrets, enriches, and ships OTLP
  → OTLP HTTP  →  your Berserk ingest endpoint  (e.g. http://<berserk-host>:<port>/v1/logs)
  → Berserk `default` table  (resource service.name = "claude-code")
  → berserk-mcp  claude_* tools
```

The forwarder is a separate component; any tool that tails Claude Code's JSONL
and emits OTLP logs with the attributes below will work. This repo only provides
the query side.

## Expected attributes

Each JSONL line becomes one OTLP log record. The tools read these promoted
attributes:

- `claude.type` — `user`, `assistant`, `system`, …
- `claude.session_id` — group by this to scope to one session
- `claude.message_role` — `user` / `assistant` (API perspective)
- `claude.message_model` — model id for the turn
- `claude.tool_names` — comma-joined tool names used in an assistant turn (e.g. `Bash,Read`)
- `claude.error` — `"true"` (string) when the record is an error / a tool result failed

## Tools

| Tool | What it answers |
|---|---|
| `claude_recent` | Recent activity: timestamp, type, role, model, tools, error flag. Default 1h. |
| `claude_sessions` | Per-session rollup: events, first/last seen, assistant turns, tool turns, errors. Default 6h. |
| `claude_tools` | Tool-use histogram — how often each tool was used. Default 6h. |
| `claude_errors` | Failed tool results (`is_error`) with a body snippet. Default 6h. |
| `claude_search` | Full-text substring search across message/tool bodies. Default 6h. |

`claude_search` rejects quotes, pipe, backslash, and backtick in the term
(KQL-injection guard).

## Why the windows are bounded

In Berserk's `default` table, only `timestamp` is indexed, and `claude-code`
records share the table with the rest of your telemetry. Bounded-window queries
(≤ ~6h) stay fast; multi-day dynamic aggregations can time out. That's why every
`claude_*` tool defaults to a short window — widen it explicitly with `since`
when you need to, knowing it costs a wider scan.

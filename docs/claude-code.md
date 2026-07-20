# Claude Code telemetry tools

`berserk-mcp` ships Claude-lane tools that mine [Claude Code](https://claude.com/claude-code)
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
| `claude_loop_check` | Loop detector: repetition ratio, top repeated call, error-retry count, and verdict per session. Default 6h. |
| `claude_model_fit` | Model-fit heuristic: flags frontier models on trivial sessions and cheap models on complex/repetitive sessions. Default 6h. |
| `claude_token_burn` | Token-burn proxy: estimated tokens, burn per progress unit, and combined high-burn/loop verdict. Default 6h. |

`claude_search` rejects quotes, pipe, backslash, and backtick in the term
(KQL-injection guard).

## Headless agent report

For cron/systemd alerting, run all three analytics checks in one pass:

```bash
berserk-mcp --agent-report --since "6h ago"
```

The command prints a text summary and exits non-zero if it sees a likely loop,
an underpowered session, or high burn. Token burn uses the forwarder's
`claude.tokens_input` and `claude.tokens_output` attributes when present and a
clearly labeled body-length estimate for sessions where they are absent. Alert
transport is intentionally out of scope for this repo; pipe stdout/stderr to
your homelab wrapper.

## Why the windows are bounded

In Berserk's `default` table, only `timestamp` is indexed, and `claude-code`
records share the table with the rest of your telemetry. Bounded-window queries
(≤ ~6h) stay fast; multi-day dynamic aggregations can time out. That's why every
`claude_*` tool defaults to a short window — widen it explicitly with `since`
when you need to, knowing it costs a wider scan.

## Phase J live-verification checklist (pending `bzrk login`)

`claude_cost_report`, `claude_session_deep_dive`, and
`claude_workflow_insights` (added 2026-07-20) are unit-tested against
stubbed telemetry but not yet live-verified. After authenticating, run
the J0 probe and record findings here:

1. Presence rates over 7d for: `claude.session_id`,
   `claude.tokens_input` / `claude.tokens_output` (or the configured
   attr names), `claude.tool_names`, `claude.message_model`,
   `claude.type`, and any file-target attribute (candidates:
   `claude.tool_input.file_path`, `claude.file_target`, tool-input
   payloads inside `body`).
2. Run each tool against real telemetry; confirm the output shape and
   record the date + one example row here (per the v1.14 trace-tools
   precedent).
3. `claude_cost_report` gating: per-project attribution stays labeled
   `(unattributed)`-only unless the probe finds a usable file-path
   signal; note the outcome either way.
4. The daily aggregation in `claude_cost_report` uses
   `bin(timestamp, 1d)` with `summarize` over a 7d default window —
   verify it completes within the CLI timeout given the bounded-window
   caveat above; if it times out, narrow the default and note it.

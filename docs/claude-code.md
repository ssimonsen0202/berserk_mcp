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

In Berserk's `default` table, `timestamp` has range pruning and common
dimensions such as `resource['service.name']` and `metric_name` have shard/bloom
indexes. `claude-code` records still share the table with the rest of your
telemetry, so bounded-window queries (≤ ~6h) remain the safe default; widen it
explicitly with `since` when you need to, knowing it costs a wider scan. See the
[KQL performance guide](kql-performance-guide.md) for the live verification
details and query-author checklist.

## Phase J live-verification checklist (live-verified 2026-07-22)

`claude_cost_report`, `claude_session_deep_dive`, and
`claude_workflow_insights` (added 2026-07-20) were unit-tested against
stubbed telemetry and have now been run against the live `homelab`
Berserk instance. Findings:

1. **Attribute presence, 7d window** (11,134 `claude-code` events):
   `claude.session_id` 11,130/11,134 (99.96%), `claude.type` 11,134/11,134
   (100%), `claude.tokens_input`/`claude.tokens_output` 5,313/11,134 each
   (47.7% — the rest fall back to the body-length estimate, exactly as
   designed), `claude.tool_names` 2,611/11,134 (23.4%, only present on
   assistant turns that called a tool, as expected). **File-target
   attributes are absent entirely**: `claude.tool_input.file_path` and
   `claude.file_target` were both 0/1,779 in a 24h sample — this
   forwarder does not currently emit either candidate.
2. **Per-tool live run** (2026-07-22, `homelab` endpoint):
   - `claude_cost_report(since="7d ago", group_by="day")` — **76.8s**,
     real output: `verdict=burn-growing (slope +26.1%/day)`, 8 daily
     buckets with exact/estimated token labels, event and error counts
     per day.
   - `claude_cost_report(since="24h ago", group_by="project")` —
     29.9s, output: `(unattributed): ~3721269 tokens across 1793
     events` — see finding 3.
   - `claude_session_deep_dive("1775e12f-d0ea-4edd-a690-0578e90d5efe",
     since="7d ago")` — 14.4s, output:
     `loop=healthy, ~315 tokens (estimated)` with one contiguous
     no-tool phase example.
   - `claude_workflow_insights(since="24h ago")` — 110.0s (real tool
     sequences, e.g. `Bash→Bash x102`; `Error hotspots: none`;
     top-decile burn target identified). **`since="7d ago"` (this
     tool's registered default) timed out at 120s and did not
     complete** — see finding 4.
3. **Per-project attribution gating: confirmed correct.** With zero
   file-target signal present (finding 1), `claude_cost_report
   group_by=project` correctly stays `(unattributed)`-only rather than
   guessing — the gating logic behaves exactly as designed on this
   deployment's real (attribute-sparse) data.
4. **Timeout finding — action needed.** `claude_cost_report`'s 7d
   `bin(timestamp, 1d)` aggregation completes in ~77s, comfortably
   under the 120s default `BZRK_TIMEOUT` — no change needed there.
   **`claude_workflow_insights` is a different story: its own
   registered default window (7d) reliably times out at 120s on this
   real deployment.** Narrower windows are not uniformly faster,
   either — `24h ago` (1,778 events, no cap hit) took 110.0s, while
   `2d ago` and `3d ago` (both capped at the same 2,000-event ceiling)
   took only 25.8s and 52.7s. This suggests the most recent ~24h of
   data sits in a less-optimized "hot" partition that scans slower
   per-row than the older, settled data the wider windows mostly read
   from before hitting the event cap. **Recommended follow-up:**
   narrow `claude_workflow_insights`'s default `since` from `7d ago` to
   something in the `2d ago`–`3d ago` range (both returned complete,
   meaningful tool-sequence and burn data well within budget), or
   raise `BZRK_TIMEOUT` specifically for this tool. Not yet changed in
   code — flagging here per the checklist's own instructions pending a
   decision on which fix to take.

# MCP 2026-07-28 adaptation baseline

This document records the Phase 0 baseline for adapting berserk-mcp to the
newer MCP `2026-07-28` specification.

## Current compatibility baseline

berserk-mcp currently implements MCP `2025-06-18` over stdio using
newline-delimited JSON-RPC 2.0. The current production-compatible lifecycle is:

- `initialize`
- `notifications/initialized`
- `ping`
- `tools/list`
- `tools/call`

The server advertises `2025-06-18` from `initialize`, exposes the role-filtered
tool set through `tools/list`, and returns text tool results through
`tools/call`.

## Target adaptation

The target adaptation is additive dual-era support. The current `2025-06-18`
stdio behavior must remain available while `2026-07-28` behavior is introduced
behind an explicit compatibility path.

Planned `2026-07-28` work:

- add protocol-mode helpers and feature-gated modern behavior;
- add `server/discover`;
- add modern result envelopes with `resultType`;
- add `structuredContent` and `outputSchema` for high-value reporting tools;
- add safe tool-list cache hints where applicable;
- evaluate input-required flows for expensive queries and FinOps attribution;
- evaluate Tasks and HTTP transport only after the core modern protocol path is
  stable.

## Non-goals for Phase 0

Phase 0 does not implement MCP `2026-07-28`. It intentionally freezes the
legacy behavior so later phases can prove they did not break existing Claude
Desktop, Claude Code, or generic `2025-06-18` stdio clients.

Phase 0 also does not add Streamable HTTP, Tasks, Apps, or modern resources.
Those are separate follow-on phases.

## Phase 0 regression coverage

Phase 0 adds explicit tests for the legacy contract:

- `initialize` returns only the current `2025-06-18` shape;
- `server/discover` is not accidentally enabled in legacy mode;
- `tools/list` remains a plain `{"tools": [...]}` result without modern cache
  hints;
- `tools/call` remains text-content compatible and does not accidentally switch
  to modern-only `resultType` or `structuredContent`.

These tests complement the existing JSON-RPC, role-filtering, secret-redaction,
and tool-shape regression tests.

## Phase 1 protocol-mode scaffolding

Phase 1 introduces internal constants and helpers without exposing modern MCP
behavior by default:

- `MCP_PROTOCOL_LEGACY = "2025-06-18"`
- `MCP_PROTOCOL_MODERN = "2026-07-28"`
- `SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2026-07-28")`
- `PROTOCOL_VERSION = "2025-06-18"`
- `BERSERK_MCP_ENABLE_2026_07_28=1` gates modern-mode selection

Modern mode is selected internally only when the feature flag is enabled and a
validated request carries
`_meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"`. Until
Phase 2 implements `server/discover`, this mode-selection hook does not change
the observable legacy RPC contract.

## Phase 2 `server/discover`

Phase 2 adds the modern discovery method behind
`BERSERK_MCP_ENABLE_2026_07_28=1`.

When disabled, `server/discover` still returns `Method not found`, preserving
the Phase 0 legacy baseline. When enabled, a valid modern request returns:

- `resultType: "complete"`;
- `supportedVersions: ["2026-07-28", "2025-06-18"]`;
- `capabilities.tools.listChanged: false`;
- `_meta["io.modelcontextprotocol/serverInfo"]`;
- the existing role-aware server instructions;
- conservative private cache hints.

The discovery result intentionally does not inline the tool list. Clients must
still call `tools/list` for the role-filtered tools. This avoids introducing a
second tool-visibility surface while the modern `tools/list` envelope is still
planned for a later phase.

## Phase 3 modern `tools/call` result envelope

Phase 3 adds the first modern tool result envelope while keeping legacy behavior
unchanged.

When `BERSERK_MCP_ENABLE_2026_07_28=1` is set and a `tools/call` request carries
valid modern `_meta`, the tool result includes:

- `resultType: "complete"`;
- the existing text `content` array;
- the existing `isError` flag.

Legacy `tools/call` responses do not include `resultType`. Phase 3 also does
not add `structuredContent`; that remains the Phase 4 task so structured output
can be designed per high-value reporting tool rather than bolted onto every
tool generically.

## Phase 4 structured reporting output

Phase 4 adds modern structured output for high-value AI FinOps and management
reporting tools while preserving text compatibility.

In modern mode, `tools/list` accepts valid modern `_meta` and includes
`outputSchema` for these reporting tools:

- `claude_spend_overview`
- `claude_feature_cost`
- `claude_project_economics`
- `claude_efficiency_insights`
- `claude_harness_recommendations`
- `claude_optimization_impact`
- `claude_management_report`
- `claude_generate_dashboard`

In modern `tools/call`, successful results from those tools include
`structuredContent` when the existing sanitized text envelope contains valid
fenced JSON with a `schema_version`. The text `content` remains present and
unchanged for Claude Desktop, Claude Code, and other legacy/text-first clients.

Errors and non-reporting tools do not receive synthetic structured content.

## Phase 5 tool-list cache hints

Phase 5 adds conservative modern cache hints to `tools/list`:

- `resultType: "complete"`;
- `ttlMs: 300000`;
- `cacheScope: "private"`.

The cache scope is intentionally private. Tool visibility and instructions are
role-aware and deployment-aware, so clients must not share one role's cached
tool list with another role or user context. Legacy `tools/list` remains
unchanged and does not include cache hints.

## Phase 6 input-required guidance

Phase 6 adds modern-only input-required guidance for cases where the MCP can
help the user get the intended result while protecting cluster health and
attribution quality.

Modern `tools/call` may now return `resultType: "input_required"` for:

- broad custom `search` calls where `since` is more than 24 hours and the KQL
  does not appear to include a bounding operator (`take`, `limit`, `count`,
  `summarize`, or `top`);
- `claude_feature_cost` without `feature_id`;
- `claude_project_economics` without `project_id`.

The expensive-query guard does not execute `bzrk`. It asks the client/user to
narrow the window, add a bounding operator, or retry with
`arguments.allow_expensive=true` after explicit approval. This gives an agent a
safe loop: clarify intent first, then spend cluster resources only when the
user accepts the tradeoff.

The guidance is modern-only. Legacy `2025-06-18` calls keep the previous
text-result/error behavior. Hidden role tools still return the same
`unknown tool` response and never reveal a tool-specific input-required reason.

## Phase 7 task lifecycle

Phase 7 adds a minimal modern-only task lifecycle for selected long-running
operations:

- `generate_parser`
- `run_discovery_worker`
- `claude_generate_dashboard`

Task creation is explicit. A modern client must:

1. enable MCP `2026-07-28`;
2. advertise task capability in `_meta["io.modelcontextprotocol/clientCapabilities"]`;
3. call an eligible tool with `arguments.as_task=true`.

The server then returns `resultType: "task"` with a task id. Clients can poll
with `tasks/get` and request cancellation with `tasks/cancel`.

Current task constraints are intentionally conservative:

- tasks are in-memory only and are not persisted across process restarts;
- tasks expire after one hour;
- at most 64 task records are retained;
- task results are filtered through the same MCP output redaction path before
  being stored;
- task lookup is role-scoped, so a task created in one active role is not
  visible from another role;
- cancellation is best-effort: pending/running task state is marked cancelled,
  but Python cannot safely terminate an already-running parser/dashboard worker
  thread.

Legacy clients and modern clients without task capability keep synchronous
behavior even if they pass `as_task=true`.

## Phase 8 HTTP transport safe defaults

Phase 8 adds an optional stdlib HTTP transport while preserving stdio as the
default.

Safe defaults:

- HTTP is disabled unless `BERSERK_MCP_HTTP_ENABLE=1` or `--http` is used with
  that environment setting;
- the default bind is `127.0.0.1:8765`;
- non-loopback bind requires `BERSERK_MCP_HTTP_ALLOW_REMOTE=1`;
- non-loopback bind also requires bearer auth, Host allowlisting, and CIDR
  allowlisting;
- global allow-all CIDRs such as `0.0.0.0/0` and `::/0` are rejected;
- only `POST /mcp` dispatches JSON-RPC;
- `GET /healthz` returns only a minimal status object;
- CORS is not enabled;
- request bodies are size-limited;
- concurrent requests are bounded;
- `X-Forwarded-For` is ignored unless forwarded-header mode and trusted proxy
  CIDRs are both configured.

Production/shared deployments should terminate HTTPS/TLS at a reverse proxy and
forward to loopback. See [MCP HTTP transport and reverse proxy deployment](mcp-http-reverse-proxy.md).

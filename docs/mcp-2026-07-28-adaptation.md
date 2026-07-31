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

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

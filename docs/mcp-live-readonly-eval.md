# MCP live read-only eval

Use this eval after `berserk-mcp` has been deployed and connected to a real
Berserk profile. It validates that the MCP server starts, exposes the expected
Claude feature set, and can execute safe read-only tools against the cluster.

This eval should not write to Berserk or local MCP stores.

## Command-line eval

From the repository checkout:

```bash
BERSERK_MCP_ROLE=claude python3 evals/mcp_live_smoke.py \
  --json-out /tmp/berserk-mcp-live-smoke.json
```

If you want to test an installed command rather than the local file:

```bash
BERSERK_MCP_ROLE=claude python3 evals/mcp_live_smoke.py \
  --server-command berserk-mcp \
  --json-out /tmp/berserk-mcp-live-smoke.json
```

Optional environment:

```bash
export BZRK_PROFILE=local
export BERSERK_MCP_ROLE=claude
```

Expected result:

- the MCP handshake succeeds
- Claude tools are visible
- safe KQL validates
- semicolon and source-introducing KQL are rejected
- Claude telemetry and AI FinOps tools return structured output or graceful
  empty/insufficient-data messages
- no raw sensitive markers appear in returned text

Treat a non-zero exit code as a release-blocking smoke failure until triaged.

## Instructions for Claude

Give Claude this prompt:

```text
Run the live read-only MCP smoke eval for berserk-mcp.

Use:
BERSERK_MCP_ROLE=claude python3 evals/mcp_live_smoke.py \
  --json-out /tmp/berserk-mcp-live-smoke.json

Do not change code or configuration. Summarize:
- overall pass/fail
- missing tools, if any
- failed checks
- skipped checks
- whether KQL semicolon/source-operator guards worked
- whether failures look like deployment/configuration/data gaps or code defects

Do not paste raw secrets, tokens, private hostnames, or full telemetry rows.
```

## Manual fallback

If running scripts is not available in the Claude environment, use
[`docs/claude-mcp-v1.23-live-feature-test.md`](claude-mcp-v1.23-live-feature-test.md)
as the manual MCP prompt.

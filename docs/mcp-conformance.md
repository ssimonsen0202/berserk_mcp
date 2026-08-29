# Official MCP conformance test suite results

Run: 2026-08-29, against `@modelcontextprotocol/conformance` (the
`modelcontextprotocol/conformance` repository's CLI), berserk-mcp v1.27.0,
HTTP transport (`--http`), stable default protocol version (`2025-06-18`).

This is the closest thing to an official, protocol-level correctness
certification that exists for MCP servers today -- distinct from routing or
tool-quality suitability, which has no equivalent official certification
(see [docs/model-routing-cost-validation-2026-08-23.md](model-routing-cost-validation-2026-08-23.md)
for that side of the story instead). Prior to this run, berserk-mcp's
protocol-correctness evidence was two independent security scanners (Cisco
AI Defense `mcp-scanner`, MCP-Shield) plus this project's own adversarial
regression tests -- both real, but neither is the protocol's own reference
conformance suite.

## How it was run

```bash
BERSERK_MCP_HTTP_ENABLE=1 BERSERK_MCP_HTTP_BIND=127.0.0.1:8899 \
  BZRK_BIN=/bin/true python3 berserk_mcp.py --http &

npx @modelcontextprotocol/conformance server \
  --url http://127.0.0.1:8899/mcp -o results/ --verbose
```

`BZRK_BIN=/bin/true` stands in for a real `bzrk` install -- this run only
exercises protocol plumbing (`initialize`, `tools/list`, `tools/call`
shape), never an actual query. No auth token was configured, matching a
loopback-only deployment as described in
[docs/mcp-http-reverse-proxy.md](mcp-http-reverse-proxy.md); the suite ran
directly against the unauthenticated loopback endpoint.

## Result: 6 passed, 24 failed by the suite's raw pass/fail count -- read that number carefully

The suite runs every scenario against every server regardless of which
optional capabilities that server declares. berserk-mcp is a **tools-only**
server: no `resources`, `prompts`, `logging`, `completion`, `sampling`, or
`elicitation` capability, and every tool returns text content only. 23 of
the 24 raw failures are exactly what a correctly-implemented tools-only
server should produce against those scenarios -- not bugs:

| Category | Scenarios | Why they "fail" |
|---|---|---|
| Resources | `resources-list`, `resources-read-text`, `resources-read-binary`, `resources-templates-read`, `resources-subscribe`, `resources-unsubscribe` | berserk-mcp never declares the `resources` capability. Each correctly returns `MCP error -32601: Method not found`. |
| Prompts | `prompts-list`, `prompts-get-simple`, `prompts-get-with-args`, `prompts-get-embedded-resource`, `prompts-get-with-image` | Same -- no `prompts` capability declared. Same `-32601`. |
| Logging / completion | `logging-set-level`, `completion-complete` | Same -- neither capability declared. |
| Server-initiated | `tools-call-sampling`, `tools-call-elicitation`, `elicitation-sep1034-defaults`, `elicitation-sep1330-enums` | berserk-mcp never requests sampling or elicitation from the client, by design -- every tool is a single deterministic round trip. |
| Non-text content | `tools-call-image`, `tools-call-audio`, `tools-call-embedded-resource`, `tools-call-mixed-content` | Every tool returns text only, by design -- observability answers are text, not media. |
| Progress | `tools-call-with-progress` | No tool call is long-running enough to warrant progress notifications; none are sent. |
| Logging during a call | `tools-call-with-logging` | Same as the standalone logging scenario -- capability not declared. |

None of these are required for a tools-only server, and fixing them would
mean *adding* capabilities berserk-mcp deliberately doesn't have, not
fixing a defect. They're recorded here for completeness, not as an action
item.

## The one real finding: `dns-rebinding-protection`

This is not an optional-capability question. It failed for a real reason:

```
Expected HTTP 4xx for invalid Host/Origin headers, got 200
hostHeader: "evil.example.com", statusCode: 200
```

berserk-mcp's default HTTP config (loopback bind, no
`BERSERK_MCP_HTTP_ALLOWED_HOSTS` set) accepts a request with a spoofed
`Host` header and returns the full `initialize` response. Verified live
against a real running instance (not just read from the code), and
verified the existing mitigation works when configured:
`BERSERK_MCP_HTTP_ALLOWED_HOSTS=127.0.0.1` makes the identical request
correctly return 403, confirmed by re-running the same conformance
scenario afterward (2/2 passed).

**Not fixed here** -- this doc is the measurement, per issue #76's own
scope. Filed as [#84](https://github.com/ssimonsen0202/berserk_mcp/issues/84):
whether to default the host allowlist on for a loopback bind, or make the
current opt-in an explicit, warned choice like `BERSERK_MCP_REDACT`'s
weaker modes already are.

## Required baseline: passed

The scenarios that matter for any MCP server regardless of declared
capabilities all passed: `server-initialize`, `ping`, `tools-list`,
`tools-call-simple-text`, `tools-call-error`,
`server-sse-multiple-streams` (0 passed / 0 failed -- not applicable to
this transport mode, not a failure), and the accept-half of
`dns-rebinding-protection` (legitimate loopback requests are correctly
accepted).

## Re-running this

```bash
BERSERK_MCP_HTTP_ENABLE=1 BERSERK_MCP_HTTP_BIND=127.0.0.1:<port> \
  BERSERK_MCP_HTTP_ALLOWED_HOSTS=127.0.0.1 BZRK_BIN=/bin/true \
  python3 berserk_mcp.py --http &
npx @modelcontextprotocol/conformance server --url http://127.0.0.1:<port>/mcp
```

Passing `BERSERK_MCP_HTTP_ALLOWED_HOSTS` closes the one real gap found
here; the 23 optional-capability "failures" will still show up on every
future run until berserk-mcp adds one of those capabilities (unlikely,
given the project's deliberately narrow, tools-only design) -- that's
expected, not a regression to chase.

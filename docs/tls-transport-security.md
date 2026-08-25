# Transport security and TLS guidance

Use HTTPS/TLS for every non-loopback endpoint that carries a token, API key, or
telemetry payload:

- `BERSERK_LLM_HERMES_URL` may use `http://localhost` or `http://127.0.0.1`
  for a local model gateway. Non-loopback `http://` is rejected unless
  `BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE=1` is explicitly set. Treat that flag as
  a temporary private-network exception, not an enterprise default.
- `BERSERK_DISCORD_ALERT_URL` follows the same loopback/plaintext policy. Use
  `https://` when the bridge runs on another host, because
  `BERSERK_DISCORD_ALERT_SECRET` is sent as `X-Auth-Token`.
- `BERSERK_MCP_OTLP_LOGS_ENDPOINT` is stricter: non-loopback OTLP endpoints
  must use HTTPS. Plain HTTP is accepted only for loopback collectors.
- The optional HTTP MCP transport is disabled by default. If enabled, it binds
  to loopback unless `BERSERK_MCP_HTTP_ALLOW_REMOTE=1` is explicitly set, and
  remote bind fails closed without bearer auth, Host allowlisting, and CIDR
  allowlisting. Put remote/shared deployments behind HTTPS/TLS; see
  [MCP HTTP transport and reverse proxy deployment](mcp-http-reverse-proxy.md).
- The Berserk cluster endpoint itself is configured inside the `bzrk` CLI
  profile via `bzrk login`. berserk-mcp shells out to `bzrk` and never reads
  the stored bearer token or profile URL, so operators must ensure the CLI
  profile points at an HTTPS Berserk endpoint in shared or production
  deployments.

Code enforcement already covers the endpoints owned by berserk-mcp: URL
schemes are allowlisted, embedded credentials and control characters are
rejected, redirects are not followed, response bodies are bounded, and remote
OTLP requires HTTPS.

See [Configuration reference](configuration.md) for every environment
variable named above, and [Security](../README.md#security) for the broader
threat model these controls sit inside.

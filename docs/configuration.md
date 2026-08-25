# Configuration reference

All configuration is via environment variables. All are optional:

| Variable | Default | Purpose |
|---|---|---|
| `BZRK_BIN` | `bzrk` | Trusted path/name of the Berserk CLI binary. Prefer an absolute path; it is required if a bare Windows name resolves inside the current working directory. |
| `BZRK_PROFILE` | `local` | The `bzrk` profile to query. |
| `BZRK_TIMEOUT` | `120` | Per-query timeout, seconds (worker and generation paths). |
| `BERSERK_WORKER_JITTER_SECONDS` | `7200` | Maximum random startup delay for `--worker`; set `0` to disable. Interactive MCP calls are never jittered. Derived from the 100-worker collision sweep. |
| `BERSERK_MCP_TOOL_BUDGET_SECONDS` | `10` (clamped to `BZRK_TIMEOUT`) | Base per-query budget for interactive `tools/call`, for short windows; derived from a five-repeat deployment p95 sweep. Scales up for wider `since` windows — see `BERSERK_MCP_BUDGET_PER_HOUR_SECONDS`. Timeout errors advise a narrower window or raising these values. |
| `BERSERK_MCP_BUDGET_PER_HOUR_SECONDS` | `0.5` | Added to the base budget per hour of the query's `since` window (capped at `BZRK_TIMEOUT`) — a 72h query gets ~46s, not the 10s base. Set `0` to restore a flat budget regardless of window size (v1.19.0; see [release notes](releases/v1.19.0.md)). |
| `BERSERK_MCP_FAIL_COOLDOWN_SECONDS` | `30` | Suppress identical timeout retries within one MCP process; `0` disables. |
| `BERSERK_MCP_CACHE_TTL_SECONDS` | `120` | TTL for allowlisted read-only rollup results; derived from the synthetic trace replay; `0` disables. |
| `BERSERK_MCP_KQL_VALIDATION` | `warn` | KQL validation policy: `off`, `warn`, or `strict`. `warn` rejects malformed/control-command queries and reports performance warnings on arbitrary `search`; `strict` also blocks high-risk arbitrary and generated queries. |
| `BERSERK_MCP_KQL_LIVE_VALIDATION` | `0` | Enables `validate_kql` with `mode=live`. Live validation executes a bounded read-only query and can consume query budget. |
| `BERSERK_MCP_MAX_CONCURRENT_QUERIES` | `2` | Maximum in-process Berserk queries at once. `0` disables the local guard, which weakens retry-storm protection. |
| `BERSERK_MCP_KQL_MAX_CHARS` | `50000` | Maximum user-supplied KQL length accepted by validation. |
| `BERSERK_MCP_KQL_MAX_ROWS` | `2000` | Recommended maximum arbitrary-query result bound used by validation warnings. |
| `BERSERK_MCP_KQL_STATS` | `auto` | `off`, `auto`, or `required`; controls whether live validation asks the CLI for `--stats`. Missing or unrecognized stats are reported as unavailable, never invented. |
| `BERSERK_MCP_MAX_RESULT_BYTES` | `10485760` | Hard cap on captured successful `bzrk` stdout. Overflow kills and reaps the child and asks the caller to narrow the query. |
| `BERSERK_TABLE` | `default` | The Berserk table to query. |
| `BERSERK_MCP_LEARNED_PATH` | platform config dir | Where saved queries persist (`~/.config/berserk-mcp/learned.json` on Linux). |
| `BERSERK_MCP_ROLE` | `all` | Active role lane: `sre`, `soc`, `claude`, `ops`, or `all`. Controls tool visibility and primer injection. |
| `BERSERK_MCP_PRIMERS_DIR` | adjacent `primers/` dir | Optional absolute directory containing `<role>.md`. When explicitly set for an active lane, a missing/unreadable primer is a startup error. |
| `BERSERK_MCP_REDACT` | `redact` | Output handling: `redact` (safest, default), `flag`, or `off`. An unrecognized value fails closed to `redact`. Setting `flag` or `off` logs a startup warning, because both weaken the default. |
| `BERSERK_MCP_REDACT_ENTROPY` | unset | Set to `true` to enable high-entropy token detection. |
| `BERSERK_MCP_REDACT_PII` | unset | Comma-separated PII checks: `email,ipv4,ipv6,credit_card`. |
| `BERSERK_MCP_FINOPS_REDACT_ENTROPY` | `0` | Enable high-entropy filtering for AI FinOps free text. Format-valid structural IDs remain stable; secrets and PII are always redacted. |
| `BERSERK_MCP_PSEUDONYM_KEY` | generated private key | Optional deployment-scoped HMAC secret for owner pseudonyms. If unset, a random key is created as `pseudonym.key` in the per-user config directory. Keep it stable for longitudinal joins and treat pseudonyms as personal data. |
| `BERSERK_MCP_INGESTION_CATALOG` | adjacent catalog | Optional path to an alternate `ingestion_catalog.json`. |
| `BERSERK_MCP_TOKENS_IN_ATTR` | `claude.tokens_input` | Claude-Code attribute holding input tokens. Override this if your forwarder emits a different name — for example `claude.usage.input_tokens`. A mismatch just falls back to the body-length estimate. |
| `BERSERK_MCP_TOKENS_OUT_ATTR` | `claude.tokens_output` | Claude-Code attribute holding output tokens (see above). |
| `BERSERK_MCP_PROJECT_MARKERS` | `src,tests,lib,pkg` | Path segments that mark "inside a project" for `claude_cost_report` per-project attribution. The directory before the first marker segment is the project name. |
| `BERSERK_MCP_BUSINESS_STORE_PATH` | platform config dir | Absolute path to the private, atomically updated feature catalog and developer-effort store. New private directories are restricted to the current user; existing directory permissions are never changed. |
| `BERSERK_MCP_RECOMMENDATION_STORE_PATH` | platform config dir | Absolute path to the private harness-decision audit store. Owner and rationale values are hashed before persistence. |
| `BERSERK_MCP_REPORT_DIR` | platform config `reports/` | Absolute publication directory for generated Markdown/HTML dashboards. Snapshot filenames cannot contain path components. Its existing ACL/mode remains operator-owned. |
| `BERSERK_MCP_PRICING_CATALOG_PATH` | packaged catalog | Absolute path to an alternate effective-dated pricing catalog. Unknown models remain unpriced. |
| `BERSERK_MCP_OTLP_LOGS_ENDPOINT` | `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` or unset | Optional OTLP/HTTP logs endpoint for `engineering-work` and recommendation-audit records. Non-loopback endpoints must use HTTPS. |
| `BERSERK_MCP_OTLP_HEADERS` | `OTEL_EXPORTER_OTLP_HEADERS` or unset | Comma-separated `name=value` OTLP HTTP headers. Malformed items fail loudly and cannot override JSON `Content-Type`. |
| `BERSERK_MCP_HTTP_ENABLE` | `0` | Optional HTTP transport. Disabled by default; stdio remains the normal Claude Desktop/Claude Code path. |
| `BERSERK_MCP_HTTP_BIND` | `127.0.0.1:8765` | HTTP bind address when HTTP is enabled. Keep loopback when using a local reverse proxy. |
| `BERSERK_MCP_HTTP_ALLOW_REMOTE` | unset | Required to bind HTTP to a non-loopback address. Remote bind also requires auth, Host allowlist, and CIDR allowlist. |
| `BERSERK_MCP_HTTP_AUTH_TOKEN` | unset | Bearer token for HTTP requests. Required for non-loopback bind and recommended behind a reverse proxy. |
| `BERSERK_MCP_HTTP_ALLOWED_HOSTS` | unset | Exact Host header allowlist. Required for non-loopback bind; wildcards are intentionally unsupported. |
| `BERSERK_MCP_HTTP_ALLOW_CIDRS` | `127.0.0.1/32,::1/128` | Source IP/CIDR allowlist. Global allow-all CIDRs such as `0.0.0.0/0` and `::/0` are rejected. |
| `BERSERK_MCP_HTTP_MAX_REQUEST_BYTES` | `1048576` | Maximum HTTP JSON request body size. Oversized requests return HTTP 413. |
| `BERSERK_MCP_HTTP_MAX_CONCURRENT_REQUESTS` | `8` | Maximum concurrent HTTP requests admitted to MCP dispatch. Excess requests return HTTP 429. |
| `BERSERK_MCP_HTTP_USE_FORWARDED_FOR` | unset | Trust `X-Forwarded-For` only when the socket peer is in `BERSERK_MCP_HTTP_TRUSTED_PROXY_CIDRS`. Disabled by default. |
| `BERSERK_MCP_HTTP_TRUSTED_PROXY_CIDRS` | unset | CIDRs for reverse proxies whose forwarded client IP should be trusted. Required when forwarded-header mode is enabled. |

Parser-factory (LLM parser generation) has its own env vars — see
[Parser factory](parser-factory.md).

The CanonLoom bridge has its own two env vars (`CANONLOOM_SERVER_URL`,
`CANONLOOM_API_KEY`) — see [CanonLoom bridge](canonloom-bridge.md).

For putting a non-loopback endpoint (HTTP transport, OTLP, Hermes, Discord)
behind TLS, see [Transport security and TLS guidance](tls-transport-security.md).

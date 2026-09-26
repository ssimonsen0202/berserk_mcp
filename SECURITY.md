# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting: **Security → Report a vulnerability**. Include the
impact, reproduction steps, and any relevant configuration.

## Trust boundaries

berserk-mcp treats MCP clients, telemetry values, provider responses, configured
HTTP endpoints, and operator-supplied filesystem paths as separate boundaries.
The operator controls environment variables, credentials, and deployment ACLs.
`tools/call` rejects any argument name the tool's input schema does not declare,
and lists the valid names, so a misspelled filter cannot silently widen a query.
The only exceptions are the protocol-level `as_task` and `allow_expensive`.
A hidden tool answers `unknown tool` before its arguments are checked, so the
error never reveals its schema.
The Berserk cluster and `bzrk` authentication configuration remain outside this
project.

The most useful areas for security review are:

- KQL validation and the final `bzrk` execution boundary.
- Free-text values interpolated into fixed queries.
- Parser-factory prompts built from untrusted telemetry.
- MCP, HTTP, OTLP, Discord, dashboard, and BI egress paths.
- Atomic JSON stores, publication directories, and cross-process locks.

Public documentation uses neutral deployment examples. Real hostnames,
service-unit inventories, private profile names, and live session identifiers
belong in private operator documentation. A tracked-file regression test scans
README, policy files, primers, docs, dashboards, eval plans, and catalogs for
known deployment markers and UUID-shaped live identifiers.

## Query and process execution

All subprocesses use argv lists. The project forbids `shell=True`, `eval`,
`exec`, `compile`, `os.system`, and string-form process arguments; an AST-based
regression test scans every tracked Python module for these patterns.

`BZRK_BIN` is resolved once to an absolute path. On Windows, a bare executable
that resolves inside the MCP client's current working directory is rejected to
prevent executable planting. Operators should set an absolute trusted path on
Windows.

Arbitrary KQL must start with the configured table and must not read any other
source. The final execution boundary (`_kql_boundary.check`, called by
`bzrk_search` and by `validate_kql` in live mode) rejects any semicolon, including one inside a string literal, and rejects control
commands before spawning `bzrk`. It also rejects source-introducing operators and
functions (`union`, `join`, `lookup`, `evaluate`, `find`, `search`, `invoke`,
`externaldata`, `toscalar(`, `table(`, and similar) anywhere outside string
literals, and any non-literal right operand of `in`, `has`, `has_any` and the
other operators that accept a tabular expression (so a column-to-column
comparison with these operators is refused too). String literals are delimited the
way the Kusto lexer reads them, including verbatim, obfuscated and multi-line
forms; an unterminated literal is refused. These checks remain active when
`BERSERK_MCP_KQL_VALIDATION=off`; that setting disables advisory/static policy,
not the execution boundary or query concurrency guard.

Residual risk: a stored function called in scalar context (`extend v = Fn()`)
can read any table the `bzrk` identity can read, and a pattern-based check cannot
tell a stored function from a built-in one. Restrict the `bzrk` profile's
database permissions to the configured table where that matters.

Successful `bzrk` stdout is captured incrementally and capped by
`BERSERK_MCP_MAX_RESULT_BYTES` (10 MiB by default). On overflow the child is
killed and reaped, and the caller receives an actionable error. Diagnostics are
separately bounded and authentication failures always return a constant message.
The authentication check reads all of stderr while it streams, including bytes
past the retained diagnostic cap, so a late marker cannot turn an exit-0 failure
into an empty success. If a stream could not be read to the end, the call fails.

## Untrusted telemetry and redaction

Query results can contain attacker-controlled log text. Treat all returned data
as data, not instructions. Redact secrets before ingest whenever possible and
rotate any credential that reached telemetry.

Telemetry is wrapped in `<untrusted_log_data>`, model-authored saved-query
descriptions in `<generated-description>`, and parser-factory samples in
`<sample-data>`. Before wrapping, `_tag_guard` decodes HTML entities, JSON-style
(`\u003c`, `\x3c`, `\/`) and URL escapes (up to 8 nested levels) and NFKC forms, and neutralises any
opening or closing tag of the fence that the decoded text contains. Text that
is still encoded after 8 levels has its escape characters broken instead.

`BERSERK_MCP_REDACT=redact` is the default MCP output policy. `flag` and `off`
are explicit weaker modes and emit a startup warning. Entropy and selected PII
checks can be enabled separately.

Parser generation sends bounded, redacted samples and allowlisted structural
keys to providers. Generated KQL is validated, bounded, execution-verified, and
cannot silently replace a human query. A generated query is stored as pending
and the small tier cannot see or run it until an operator approves it with
`berserk-mcp --approve-generated <name>`; no tool can approve one. A regenerated
query, or an older generated entry without a status, counts as pending. The
gate covers everything the parser-factory pipeline writes; a deep-tier agent's
`save_query` stays trusted, as that tier may author any query. Provider errors expose only their status,
not response bodies or request credentials.

AI FinOps output always applies secret and PII redaction. Stable structural IDs
are preserved only when they match the field's expected format, so BI joins and
recommendation decisions remain deterministic. Optional high-entropy filtering
for FinOps free text is controlled by
`BERSERK_MCP_FINOPS_REDACT_ENTROPY`; it does not exempt malformed IDs or secrets.
Runs of backticks in imported or telemetry-derived strings are broken before
model-facing Markdown/JSON fences are rendered, so data cannot terminate a
fenced block and become adjacent instruction-like prose.

Feature and recommendation-decision owners are HMAC-pseudonymised before local
persistence using a per-deployment key. If no key is supplied through
`BERSERK_MCP_PSEUDONYM_KEY`, a random private key is generated in the per-user
configuration directory. These stable pseudonyms remain personal data; apply
the same authorization, retention, and deletion policy used for the source
management records. Recommendation rationale remains stored as a one-way hash.

The shipped Grafana dashboards are fixed-window, whole-dataset aggregate views
and declare no decorative template variables. Add data-source-specific filters
only when every applicable query enforces them. Dashboard variables are not an
authorization boundary; tenant and team isolation belongs in server-side query
policy and data-source or folder access controls.

The Discord bridge is an optional worker-notification path, not a general raw
query-result sink. Every alert is forced through deterministic secret and PII
redaction before its transport-size cap, regardless of the weaker MCP output
mode an operator may have selected. Keep the bridge loopback or protect it with
TLS and access controls. Source-side redaction remains required for telemetry
that should never leave the cluster.

## Outbound HTTP

LLM providers, Hermes model discovery, OTLP export (including the Codex ingestion
adapter and the OpenRouter webhook forwarder and backfill), the Discord bridge,
and the eval harness use one stdlib-only HTTP implementation. It:

- accepts only absolute `http://` or `https://` URLs;
- rejects controls, embedded credentials, malformed ports, and fragments;
- permits plaintext HTTP only on loopback unless the LLM/Discord operator makes
  the documented private-network opt-in (the OpenRouter forwarder and backfill
  take `--allow-plaintext-remote` instead). Opted-in remote plaintext still
  honours `http_proxy`/`HTTPS_PROXY`: unset them or set `no_proxy` for the
  private host, or a proxy will see the plaintext;
- always requires HTTPS for non-loopback OTLP collectors and CanonLoom
  servers, whatever the LLM/Discord opt-in says;
- never follows redirects, so credentials cannot be forwarded to a `Location`;
- never sends a request that passed as loopback through a proxy, so an ambient
  `http_proxy` cannot carry loopback plaintext or its credentials off the host;
- validates header names and values, keeps JSON `Content-Type` authoritative,
  and fails on malformed OTLP header items; and
- bounds every response before parsing or discarding it.

API keys are read from the environment only. The optional Hermes endpoint is
stored in the private local configuration; keys are never written there.

The Berserk cluster endpoint is outside this HTTP client boundary. It is owned
by the `bzrk` CLI profile configured with `bzrk login`; berserk-mcp shells out
to `bzrk` and does not read the stored profile URL or bearer token. Production
operators should configure the CLI profile with an HTTPS Berserk endpoint and
avoid plaintext cluster access except on loopback-only test deployments.

## Filesystem stores and publication outputs

All store paths are absolute, traversal-free, and control-character-free. The
same shared validator covers learned queries, parser state, schema snapshots,
AI FinOps stores, reports, primer overrides, and BI output paths.

Private JSON stores are atomically replaced using unique temporary files. On
POSIX, files created by the module are `0600` and directories it creates are
`0700`. On Windows, a protected DACL grants full control only to the current
user. ACL tightening failures are warned about without corrupting an otherwise
successful store write.

berserk-mcp never changes permissions on a directory it did not create. It warns
when an existing POSIX private-store directory is group/world-accessible. BI
exports and generated management reports are publication outputs: their
directory and file policy is owned by the operator so service-account access is
not silently removed.

An explicit `BERSERK_MCP_PRIMERS_DIR` must be an absolute validated path and
must contain a readable `<role>.md` for an active role. Misconfiguration fails
startup instead of silently removing high-trust role guidance.

### Accepted lock limitation

JSON read-modify-write cycles use an atomic lockfile. A lock older than 30
seconds is treated as abandoned so a crashed process cannot deadlock future
writes. A process suspended longer than 30 seconds could have its lock broken;
if it later resumes, two writers could race and one update could be lost. Slow
LLM work is deliberately performed outside these critical sections. Deployments
that routinely suspend processes should avoid overlapping worker runs or place
the stores on infrastructure with an external single-writer schedule.

## Test expectations

Security changes must remain standard-library-only and offline. Loopback HTTP
servers are allowed in tests; live Berserk or real provider credentials are not.
Each behavioural section above is cited by at least one test with a
`# Covers SECURITY.md#<section-slug>` comment; `tests/test_security_doc_coverage.py`
fails when a section has no citing test or a citation names a missing section.
Security-critical functions are listed in `tests/security_reviews.json` with the
fingerprint of their code at last review; `tests/test_security_reviews.py` fails
when that code changes until it is re-reviewed and the entry updated.
CI also runs Trail of Bits' generic semgrep rules, pinned to one commit, over
every tracked file, including README, docs and configs
(`scripts/tob_semgrep_gate.py`). It fails on insecure transport in example
commands (disabled TLS verification, plaintext non-loopback URLs, SSH without
host-key checks), and it fails closed if the rules do not fire on a planted
canary.
Run both commands because they exercise different import/global-state paths:

```bash
python tests/test_berserk_mcp.py
python -m unittest discover -s tests
```

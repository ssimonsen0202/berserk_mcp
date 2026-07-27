# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting: **Security → Report a vulnerability**. Include the
impact, reproduction steps, and any relevant configuration.

## Trust boundaries

berserk-mcp treats MCP clients, telemetry values, provider responses, configured
HTTP endpoints, and operator-supplied filesystem paths as separate boundaries.
The operator controls environment variables, credentials, and deployment ACLs.
The Berserk cluster and `bzrk` authentication configuration remain outside this
project.

The most useful areas for security review are:

- KQL validation and the final `bzrk` execution boundary.
- Free-text values interpolated into fixed queries.
- Parser-factory prompts built from untrusted telemetry.
- MCP, HTTP, OTLP, Discord, dashboard, and BI egress paths.
- Atomic JSON stores, publication directories, and cross-process locks.

## Query and process execution

All subprocesses use argv lists. The project forbids `shell=True`, `eval`,
`exec`, `compile`, `os.system`, and string-form process arguments; an AST-based
regression test scans every tracked Python module for these patterns.

`BZRK_BIN` is resolved once to an absolute path. On Windows, a bare executable
that resolves inside the MCP client's current working directory is rejected to
prevent executable planting. Operators should set an absolute trusted path on
Windows.

Arbitrary KQL must start with the configured table. The final execution boundary
rejects any semicolon, including one inside a string literal, and rejects control
commands before spawning `bzrk`. Static validation also blocks source-introducing
operators such as `union`, `externaldata`, `evaluate`, `find`, and operator-form
`search`. These checks remain active when
`BERSERK_MCP_KQL_VALIDATION=off`; that setting disables advisory/static policy,
not the execution boundary or query concurrency guard.

Successful `bzrk` stdout is captured incrementally and capped by
`BERSERK_MCP_MAX_RESULT_BYTES` (10 MiB by default). On overflow the child is
killed and reaped, and the caller receives an actionable error. Diagnostics are
separately bounded and authentication failures always return a constant message.

## Untrusted telemetry and redaction

Query results can contain attacker-controlled log text. Treat all returned data
as data, not instructions. Redact secrets before ingest whenever possible and
rotate any credential that reached telemetry.

`BERSERK_MCP_REDACT=redact` is the default MCP output policy. `flag` and `off`
are explicit weaker modes and emit a startup warning. Entropy and selected PII
checks can be enabled separately.

Parser generation sends bounded, redacted samples and allowlisted structural
keys to providers. Generated KQL is validated, bounded, execution-verified, and
cannot silently replace a human query. Provider errors expose only their status,
not response bodies or request credentials.

AI FinOps output always applies secret and PII redaction. Stable structural IDs
are preserved only when they match the field's expected format, so BI joins and
recommendation decisions remain deterministic. Optional high-entropy filtering
for FinOps free text is controlled by
`BERSERK_MCP_FINOPS_REDACT_ENTROPY`; it does not exempt malformed IDs or secrets.

Feature and recommendation-decision owners are HMAC-pseudonymised before local
persistence using a per-deployment key. If no key is supplied through
`BERSERK_MCP_PSEUDONYM_KEY`, a random private key is generated in the per-user
configuration directory. These stable pseudonyms remain personal data; apply
the same authorization, retention, and deletion policy used for the source
management records. Recommendation rationale remains stored as a one-way hash.

The Discord bridge is an optional worker-notification path, not a general raw
query-result sink. Keep the bridge loopback or protect it with TLS and access
controls. Source-side redaction remains required for telemetry that should never
leave the cluster.

## Outbound HTTP

LLM providers, Hermes model discovery, OTLP export, the Discord bridge, and the
eval harness use one stdlib-only HTTP implementation. It:

- accepts only absolute `http://` or `https://` URLs;
- rejects controls, embedded credentials, malformed ports, and fragments;
- permits plaintext HTTP only on loopback unless the LLM/Discord operator makes
  the documented private-network opt-in;
- always requires HTTPS for non-loopback OTLP collectors;
- never follows redirects, so credentials cannot be forwarded to a `Location`;
- validates header names and values, keeps JSON `Content-Type` authoritative,
  and fails on malformed OTLP header items; and
- bounds every response before parsing or discarding it.

API keys are read from the environment only. The optional Hermes endpoint is
stored in the private local configuration; keys are never written there.

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
Run both commands because they exercise different import/global-state paths:

```bash
python tests/test_berserk_mcp.py
python -m unittest discover -s tests
```

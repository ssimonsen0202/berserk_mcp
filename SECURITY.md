# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Instead, use GitHub's private vulnerability reporting:
**Security → Report a vulnerability** on this repository. That opens a private
advisory visible only to the maintainers.

Please include: what the issue is, how to reproduce it, and the impact you see.
You'll get an acknowledgement as soon as possible, and a fix or mitigation plan
once it's triaged.

## Scope notes

This server shells out to the `bzrk` CLI and authors KQL. The areas most worth
scrutiny:

- **KQL string interpolation.** Two tools interpolate user input into KQL string
  literals: `logs_for_service` (service name) and `claude_search` (search term).
  Both are validated first — `[A-Za-z0-9._-]` for the service name, and a
  reject-list of quote/pipe/backslash/backtick for the search term. A bypass of
  either guard is a valid report.
- **`search` / `save_query`** intentionally accept arbitrary KQL. KQL is
  read-only, so this is not a mutation risk by design — but reports of a way to
  reach a *write* path through Berserk are in scope.
- **Subprocess handling.** All `bzrk` invocations use an argument list (no
  `shell=True`). A path to shell interpretation would be a valid report.

The Berserk bearer token is never read or stored by this code — it lives in
`bzrk`'s own configuration. Token handling is therefore out of scope for this
project (report those to the Berserk project).

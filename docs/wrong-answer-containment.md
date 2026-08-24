# Wrong-answer containment

berserk-mcp's own controls against a *confident false negative* — an agent
reporting a clean bill of health because a query silently matched zero rows,
went stale, or was fixed by a query the tool refused to run — consolidated
under one name. Most open-source observability MCP implementations' stated
hallucination defenses (rate limiting, query timeouts, read-only execution)
protect backend stability; few address this query-result failure mode, which
is the one that actually pages someone at 4am. Each control below is
scattered through the main [README](../README.md); this list exists so it
can be reviewed, tested, and cited as one thing.

- **Field-access guidance.** OTLP resource/log attributes such as service
  and host names — the fields models most often guess wrong — live nested
  under `resource[...]`/`attributes[...]`, not as flat columns. Some fields
  genuinely are top-level (log/metric fields like `metric_name`, `value`,
  `body`, `timestamp`; trace/span fields like `trace_id`, `span_id`,
  `span_name`, `duration`, `status_code`, and more) — an exhaustive list
  goes stale as the schema grows, so the guidance doesn't attempt one; use
  `discover_schema` rather than assuming either way. A bare column name
  like `service_name` is not a KQL error — it silently matches zero rows.
  `_BASE_INSTRUCTIONS` and
  the `search` tool description both warn about this explicitly and point
  at `discover_schema` before guessing again. Prompt-level, not code-
  enforced — the query engine's own behavior can't be changed from here —
  so the control is the warning existing and staying worded precisely,
  which is why it has a locking test: `WrongAnswerContainmentTest.
  test_base_instructions_warn_about_bare_column_names`.
- **Full-text search term-boundary guidance.** `search "term"` matches whole
  delimited tokens (`_-./:` and whitespace all delimit), not substrings — a
  plural or singular mismatch (`search "journal"` vs a body containing
  `journals`) silently returns zero rows exactly like the bare-column-name
  case above, but for an unrelated reason a model would not otherwise think
  to check. `_BASE_INSTRUCTIONS` names the wildcard escape (`search
  "journal*"`) and recommends `=~`/`!~` over `tolower(field) == ...` for
  case-insensitive matching, since the latter also silently degrades query
  performance by defeating index pruning. Sourced from the
  [berserkdb/berserk-skills](https://github.com/berserkdb/berserk-skills)
  reference agent's own accumulated field notes. Locking tests:
  `test_base_instructions_warn_about_search_term_boundaries`,
  `test_base_instructions_recommend_case_insensitive_operator`.
- **KQL validation rejects blockers before execution.** A query against the
  wrong table, or one carrying a source-introducing operator, is rejected
  by static validation before it reaches `bzrk` — rather than running and
  returning an empty or unrelated result that looks like a real answer. See
  the main README's "Security" section, "Schema-grounded KQL validation";
  `WrongAnswerContainmentTest.test_validate_kql_rejects_wrong_table_prefix`
  is the containment-framed regression test. This validation can be disabled
  via `BERSERK_MCP_KQL_VALIDATION=off` (not recommended — an escape hatch for
  debugging only).
- **Schema-drift warning on saved queries.** A saved query is revalidated
  against the *current* schema every time it runs (unless `BERSERK_MCP_KQL_VALIDATION=off`,
  the same escape hatch as above, which skips this check and the hash
  comparison entirely). If the schema has
  changed since the query was saved, the response is prefixed with an
  explicit warning naming both hashes, rather than silently returning
  whatever the (possibly now-wrong) query still happens to match.
  `WrongAnswerContainmentTest.test_schema_drift_warning_fires_when_stored_hash_differs`
  proves the warning fires on drift; the companion
  `test_schema_drift_warning_silent_when_hashes_match` proves it stays
  silent otherwise, so it doesn't become noise nobody reads. **Known limitations:**
  Schema snapshots are cached for up to one hour (`DEFAULT_TTL_SECONDS` in
  `schema_registry.py`; not currently exposed as an environment variable).
  When the schema backend fails (auth error, timeout, etc.), `_schema_fetcher`
  now correctly raises rather than passing error text through as schema
  data (fixed in v1.26.0, issue
  [#32](https://github.com/ssimonsen0202/berserk_mcp/issues/32)) — a failed
  fetch falls back to a stale cache or reports `"unavailable"`, and no
  longer produces a false-positive drift warning derived from error-text
  hashing. The warning is silently skipped only when the *saved query* has
  no stored hash at all (queries saved before this feature existed).
- **The result envelope disambiguates the bare `(no rows)` sentinel.** An
  empty result used to be indistinguishable between healthy, wrong window,
  wrong tool, and a source that stopped reporting. Fixed-query tools dispatched
  through the `SIMPLE` path (e.g., `list_hosts`, `errors_by_service`) now echo
  their resolved window and, on empty results, a concrete per-tool next step
  naming a real tool or argument. Tools outside this path (e.g., `logs_for_service`,
  `search`) and environments with `BERSERK_MCP_ENVELOPE=0` return results
  unenveloped. See `docs/result-envelope-implementation-spec.md` and
  `ResultEnvelopeTest` for full coverage.
- **Returned telemetry is fenced as untrusted data.** Not a containment
  control in the same sense as the others — it defends against an agent
  *acting on an instruction smuggled into a log line*, not against a query
  silently returning the wrong thing — but it belongs in the same "can we
  trust what came back" conversation. Real telemetry rows are wrapped in an
  explicit `<untrusted_log_data>` marker before reaching the model, with a
  matching instruction to treat the content strictly as data. This fencing
  is issue #11; see `_fence_untrusted` and the `UntrustedDataFencing*` test
  classes for the full call-site coverage and threat-model detail.

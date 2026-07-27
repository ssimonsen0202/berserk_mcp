# Claude MCP v1.23 live feature test

Use this document as a prompt for Claude after the deployed `berserk-mcp` server
has been updated to v1.23.0 or newer. It is intended to validate the new Claude
lane, AI FinOps, KQL validation, dashboard, and security-boundary behavior
against the live Berserk cluster.

## Operating rules for Claude

You are testing the deployed `berserk-mcp` MCP server. Do not edit code, deploy,
or change production configuration. Prefer read-only tools. Only use write-like
MCP tools when a test below explicitly asks for them.

Record each test as:

- `PASS`, `FAIL`, or `SKIP`
- tool call used
- short evidence from the result
- any unexpected error text

If a tool is not visible, record whether the active MCP role appears to be wrong.
The expected role is `claude` or `all`; the Claude-only tools should be visible.

Use conservative time windows first (`1h ago`, `6h ago`, `7d ago`). If a result
is empty, widen once to `30d ago` and record that the wider window was needed.

Do not paste raw secrets, tokens, emails, or private hostnames into the report.
If they appear in a tool result, report that as a security failure and quote only
the redaction status, not the value.

## 1. Connectivity and role surface

Goal: confirm Claude can see the expected v1.23 tool surface.

Ask Claude:

```text
List the available berserk-mcp tools and confirm whether the Claude lane tools
are visible. I expect to see validate_kql plus the Claude tools:
claude_recent, claude_sessions, claude_tools, claude_errors, claude_search,
claude_loop_check, claude_model_fit, claude_token_burn, claude_cost_report,
claude_session_deep_dive, claude_workflow_insights, claude_spend_overview,
claude_feature_cost, claude_project_economics, claude_efficiency_insights,
claude_harness_recommendations, claude_record_recommendation_decision,
claude_optimization_impact, claude_management_report, and
claude_generate_dashboard.
```

Pass criteria:

- `validate_kql` is visible.
- Claude Code analytics tools are visible.
- AI FinOps tools are visible.
- `claude_record_recommendation_decision` and `claude_generate_dashboard` are
  visible but recognized as write-like tools.

## 2. KQL validation and execution-boundary guards

Goal: confirm v1.20/v1.22 KQL validation and hard execution guards are active.

Ask Claude to run these validation checks:

```text
Use validate_kql in static mode for each query below and summarize whether it is
accepted or rejected. Do not run arbitrary search unless explicitly requested.

1. default | take 5
2. default | take 1; .show tables
3. default | where body contains 'a;b' | take 1
4. default | union default | take 5
5. default | externaldata(x:string)['https://example.invalid/a.csv'] | take 5
6. default | evaluate bag_unpack(attributes) | take 5
7. default | find 'password'
```

Pass criteria:

- Query 1 is valid or only reports non-blocking schema/cost warnings.
- Queries 2 and 3 are rejected because semicolons are blocked unconditionally.
- Queries 4 through 7 are rejected as source-introducing or high-risk operators.
- Claude must report that `BERSERK_MCP_KQL_VALIDATION=off`, if configured, does
  not disable the final semicolon/control-command execution boundary.

## 3. Basic Claude telemetry

Goal: confirm the existing Claude analytics still work on live data.

Ask Claude:

```text
Run a basic Claude telemetry smoke test for the last 6 hours, widening to 30
days only if empty:

1. claude_recent
2. claude_sessions
3. claude_tools
4. claude_errors
5. claude_workflow_insights

Summarize the number of rows or top-level findings from each tool.
```

Pass criteria:

- Tools return structured output, not raw table dumps only.
- Empty data is handled gracefully.
- Error snippets, if any, are bounded and redacted.

## 4. Token burn and cost accounting

Goal: confirm v1.21/v1.21.1 TokenBurn-style usage accounting and de-duplication.

Ask Claude:

```text
Analyze Claude token and cost telemetry:

1. Run claude_token_burn for the last 7 days.
2. Run claude_cost_report with group_by=day for the last 7 days.
3. Run claude_cost_report with group_by=model for the last 7 days.
4. Run claude_cost_report with group_by=project for the last 30 days.

For each result, report whether token usage is exact, estimated, or mixed. Look
for signs that repeated content-block rows are collapsed by message_id rather
than counted as separate billable calls.
```

Pass criteria:

- Results label exact versus estimated usage.
- Cost report includes per-day or per-model totals where data exists.
- Project attribution either returns projects or explicitly reports
  unattributed/insufficient coverage.
- No obvious duplicate multiplication from repeated content blocks.

## 5. Enterprise AI FinOps overview

Goal: confirm v1.21 enterprise AI FinOps tools produce management-ready outputs.

Ask Claude:

```text
Run AI FinOps overview checks:

1. claude_spend_overview since="30d ago" group_by="day"
2. claude_spend_overview since="30d ago" group_by="project"
3. claude_spend_overview since="30d ago" group_by="model"
4. claude_efficiency_insights since="30d ago"
5. claude_management_report scope="portfolio" since="30d ago"

Summarize spend, coverage, pricing-version information, data-quality warnings,
and the most expensive operations or agent patterns.
```

Pass criteria:

- Spend overview reports pricing and attribution coverage.
- Management report includes readable text plus a structured/versioned payload.
- Efficiency insights identify expensive operations, retries, loops, context
  growth, cache misses, or insufficient evidence.
- Empty or partial business metadata is reported as a coverage issue, not as a
  crash.

## 6. Project and feature economics

Goal: confirm project/feature reporting works when governed metadata exists and
fails usefully when it does not.

Ask Claude:

```text
From claude_spend_overview grouped by project, choose one project ID that has
activity. Run claude_project_economics for that project over 30 days.

If feature IDs are visible in the output, choose one and run claude_feature_cost
for that feature over 90 days.

If no governed project or feature metadata exists, report SKIP and include the
data-quality reason returned by the tools.
```

Pass criteria:

- Project economics reports feature/repository economics or clear missing-data
  coverage.
- Feature cost reports planned/actual developer hours, AI budget/spend,
  forecast, repositories, agents, harnesses, or a clear insufficient-data reason.
- Developer hours are not inferred from session duration.

## 7. Harness recommendations and decision safety

Goal: confirm recommendations are evidence-backed and writes are explicit.

Ask Claude:

```text
Run claude_harness_recommendations since="30d ago". Do not record a decision yet.

If recommendations exist, summarize:
- recommendation_id
- agent/harness/model/project context
- confidence
- expected amendment
- validation window
- rollback criteria
- whether owner approval is required

If no recommendation exists, report PASS if the tool explains insufficient
evidence rather than inventing an amendment.
```

Pass criteria:

- Recommendations are deterministic and have stable `rec_...` IDs.
- Tool states that it never modifies a harness.
- Each recommendation has evidence, risk, validation, and rollback guidance.
- No write occurs during this test.

Optional write test for a non-production recommendation only:

```text
If and only if the operator gives an explicit test recommendation_id, call
claude_record_recommendation_decision with decision="deferred",
owner="mcp-live-test-owner", and rationale="Live v1.23 smoke test; no production
harness change authorized." Then confirm the owner is stored/reported only as a
deployment-scoped pseudonym, not cleartext.
```

Pass criteria for optional write:

- Decision append succeeds.
- Cleartext owner is not shown in follow-up reports, BI outputs, or dashboards.
- Rationale is not returned as raw text if the implementation only stores a hash.

## 8. Optimization impact

Goal: confirm matched before/after harness comparison is safe when cohorts exist.

Ask Claude:

```text
From efficiency insights or recommendations, identify a plausible agent_profile
and two harness versions. If both are available, run claude_optimization_impact
with those values over 30 days.

If there are not two comparable harness versions, report SKIP with the missing
cohort reason.
```

Pass criteria:

- Tool returns `keep`, `rollback`, `no-material-change`, or
  `insufficient-data`.
- It uses matched cohorts and does not compare unrelated projects blindly.

## 9. Dashboard generation

Goal: confirm v1.21/v1.23 local dashboard generation, privacy handling, and
fixed-window reporting.

Ask Claude:

```text
Generate a Markdown dashboard with:

claude_generate_dashboard dashboard="portfolio" since="30d ago"
format="markdown" filename="v123-live-smoke-portfolio.md"

Then summarize the returned path, dashboard type, data freshness, privacy
warnings, and whether cleartext owner IDs, emails, prompts, commands, or session
bodies appear in the result.
```

Pass criteria:

- Dashboard is written beneath the configured report directory.
- Output is Markdown or HTML as requested.
- Dashboard contains aggregates, pricing/freshness metadata, and coverage
  warnings when applicable.
- It does not expose cleartext owner IDs, emails, raw prompts, raw commands, or
  session bodies.
- It does not claim Grafana variables enforce tenant/team filtering unless those
  filters are actually implemented server-side.

## 10. Redaction, pseudonym, and model-facing fence checks

Goal: confirm v1.23 security fixes are visible at the MCP surface.

Ask Claude:

```text
Inspect outputs from the previous FinOps and dashboard tests for:

1. owner IDs or owner-like fields
2. emails or IP addresses
3. values beginning with spreadsheet formula characters: =, +, -, @
4. triple-backtick sequences inside JSON or Markdown fenced payloads
5. raw prompts, raw commands, or full session bodies

Report any raw sensitive value as FAIL without repeating the value. Report
whether structural IDs such as recommendation_id, request_id, session_id,
feature_id, project_id, agent profile, harness version, schema hash, and dedupe
keys remain stable enough for joins.
```

Pass criteria:

- Owner identifiers are HMAC pseudonyms, not cleartext.
- Pseudonyms are treated as personal data in the report.
- Structural IDs remain stable.
- Model-facing Markdown/JSON fences are not broken by data containing backticks.
- No raw secret/PII appears in dashboard or FinOps outputs.

## 11. Search input guard smoke test

Goal: confirm free-text guardrails reject dangerous characters and excessive
inputs without shelling out.

Ask Claude:

```text
Try claude_search with a safe term such as "error" over 6 hours.

Then try claude_search with these unsafe terms and confirm each is rejected
before query execution:

1. error | take 1
2. "quoted"
3. `backtick`
4. path\\escape
5. a term containing a newline
```

Pass criteria:

- Safe search works or returns an empty result gracefully.
- Unsafe terms are rejected with validation errors.
- No raw shell quoting advice is needed because subprocess calls use argv lists.

## 12. Final report format

At the end, produce this summary:

```text
# berserk-mcp v1.23 live validation

Cluster/profile tested:
MCP role:
Date/time:
Overall result: PASS / FAIL / PARTIAL

## Passed
- ...

## Failed
- ...

## Skipped
- ...

## Security observations
- ...

## Data-quality observations
- ...

## Recommended follow-up
- ...
```

Treat any of the following as release-blocking until investigated:

- Claude lane tools are missing after deployment.
- Multi-statement KQL or source-introducing operators are accepted.
- Token/cost reports obviously double-count repeated content-block telemetry.
- Generated dashboards expose cleartext owners, emails, prompts, raw commands,
  or session bodies.
- Harness recommendations imply they changed a harness automatically.
- The MCP returns unbounded raw backend stderr or secrets in auth failures.

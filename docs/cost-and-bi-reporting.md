# Cost & BI reporting

A canonical cost and attribution layer behind the Claude analytics tools,
driven by CLI flags and a wrapper binary (`berserk-claude`) rather than
`tools/call` — this is operational tooling, not an MCP tool you invoke from
an agent. Native Claude Code OpenTelemetry is the preferred input; the
existing JSONL-forwarder attributes remain compatible. Reports normalize
input, output, cache-read, cache-creation, long-context, and chargeable
server-tool usage, then calculate a versioned public API-equivalent cost.
Claude's reported approximate cost remains a separate field. This is not an
invoice, and an unknown model is left unpriced rather than assigned a guessed
rate.

## Attribute telemetry to a feature

Launch Claude with governed work context so telemetry can be attributed to a
feature without inspecting prompts or source code:

```bash
berserk-claude --team platform --project OBS --feature OBS-142 \
  --work-item ADO-912 --repository berserk-mcp --harness-version finops-v1 \
  -- claude
```

## Import planning and actuals

Import planning and actual-hours data through the neutral CSV/NDJSON contract.
The local store is updated atomically; configure an OTLP logs endpoint to also
emit privacy-safe `engineering-work` records:

```bash
berserk-mcp --import-business-data feature --input /absolute/path/features.csv
berserk-mcp --import-business-data effort --input /absolute/path/worklogs.ndjson \
  --input-format ndjson
```

Feature records use stable IDs plus optional portfolio, project, work-item,
repository, branch, pull-request, planned-hours, AI-budget, and completion
fields. Effort records contain a stable worklog ID, feature/work-item/team,
work date, actual hours, source system, and update timestamp. Developer hours
are never inferred from session duration, commits, or Claude active time.

## Export management-ready outputs

```bash
berserk-mcp --export-bi --since "90d ago" \
  --output /absolute/path/ai-finops-export --export-format csv
berserk-mcp --generate-dashboard project --identifier OBS --since "90d ago" \
  --dashboard-format html
```

The export contains seven versioned datasets and a checksum manifest. Grafana
provisioning JSON and bounded Berserk Explore queries are in
[`dashboards/`](../dashboards/README.md). Generated snapshots contain aggregates,
coverage, pricing version, freshness metadata, and deployment-scoped owner
pseudonyms where management attribution is needed. They do not contain prompts,
code, raw commands, session bodies, cleartext owner IDs, or email addresses.
Harness amendments are recommendations only: an owner must record a decision,
deploy an immutable harness version, and use the matched-cohort impact tool
before keeping or rolling back a change.

## Access control

Private local stores use current-user-only permissions. BI exports and generated
management reports are publication outputs: berserk-mcp leaves an existing
output directory's mode or ACL unchanged so a BI service account is not locked
out. The operator owns access control for those publication directories.

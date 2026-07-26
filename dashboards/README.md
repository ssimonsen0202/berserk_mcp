# AI FinOps dashboards

The dashboard package exposes the same aggregate AI-development economics as
the Claude MCP reports and BI exports. It intentionally contains no prompts,
source code, raw commands, user email, or session bodies.

## Grafana

`grafana/` contains version-controlled dashboard JSON for:

- Executive AI Spend
- Feature Delivery Economics
- Project and Codebase Spend
- Agent and Harness Efficiency
- Data Quality and Governance

Import the JSON through Grafana or provision it with your normal dashboard
deployment process. Configure the Berserk data source after import. The
queries use bounded windows and aggregate projections. Template variables
cover team, project, repository, feature, model, agent, harness, and date
range where relevant.

These dashboards read Claude's emitted `cost_usd` as reported approximate
cost. For independently calculated, effective-dated public API-equivalent
cost, use `claude_spend_overview`, a generated dashboard, or the BI export.
That distinction prevents an operational chart from being mistaken for a
provider invoice.

Management access should default to team, project, and feature aggregates.
Restrict session-level drilldowns to authorized engineering users, and apply
the same retention and access policy used for the underlying Berserk table.

## Berserk Explore

`berserk/ai-finops.kql` is a copy-ready Explore pack for daily spend,
project/feature spend, cache effectiveness, developer hours, attribution
quality, and harness impact. Each query includes a fixed 30-day bound,
aggregate output, a result limit where needed, and a `render` clause.

Berserk Explore is the native chart drilldown surface; it is not treated as a
multi-panel workbook or a materialized reporting layer. Grafana and the BI
datasets are the persistent management surfaces.

## BI datasets

Generate CSV or NDJSON files plus a checksum manifest:

```bash
berserk-mcp --export-bi --since "90d ago" \
  --output /absolute/path/ai-finops-export --export-format csv
```

The stable schema version `1.0` publishes:

- `ai_usage_daily`
- `feature_cost_snapshot`
- `project_cost_snapshot`
- `human_effort_daily`
- `agent_harness_efficiency`
- `harness_recommendation_status`
- `attribution_quality`

Files are replaced atomically. The manifest identifies the extraction time,
query window, pricing catalog, row count, and SHA-256 checksum for every
dataset. Preserve the previous successful directory or ingest transactionally
in the downstream BI platform if snapshot history is required.

## Claude Code snapshots

`claude_generate_dashboard` and the matching CLI mode generate aggregate
Markdown or self-contained HTML with inline SVG. Set
`BERSERK_MCP_REPORT_DIR` to an absolute, access-controlled directory. Output
filenames are simple basenames and cannot escape that directory.


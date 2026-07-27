# Claude Code telemetry and AI FinOps

`berserk-mcp` ships a Claude lane for developer-workflow observability,
AI-development economics, and governed harness optimization. The lane is
active when Claude Code telemetry is sent to Berserk with
`resource['service.name'] == 'claude-code'`. Other berserk-mcp lanes work
without this telemetry.

## Collection paths

Native Claude Code OpenTelemetry is the preferred source:

```text
Claude Code native OTel metrics/events
  → your OTel collector and ingest controls
  → Berserk `default` table (`service.name=claude-code`)
  → aggregate-first berserk-mcp `claude_*` tools
```

The existing JSONL forwarder remains supported:

```text
Claude Code JSONL (`~/.claude/projects/*/*.jsonl`)
  → a redacting OTLP forwarder
  → Berserk `default` table (`service.name=claude-code`)
  → berserk-mcp `claude_*` tools
```

Collection and ingest are deployment responsibilities. Keep prompt, source
code, command, file-content, and user-email capture disabled or redacted. The
FinOps reports consume aggregate usage and governed identifiers, not those
payloads.

## Normalized attributes

The normalizer accepts native OTel names and the legacy promoted attributes.
Important signal families are:

- correlation: session, interaction, request, and parent-agent identifiers;
- usage: input, output, cache-read, 5-minute/1-hour cache-creation, and
  long-context token classes;
- cost: Claude-reported approximate cost and independently calculated public
  API-equivalent cost;
- execution: model, query source, agent/subagent, harness version, tool/MCP
  name, duration, result tokens, success, retry, and error;
- outcomes: active time, lines added/removed, commits, pull requests, and
  work-item completion when emitted;
- business context: portfolio, team, project, feature, work item, cost center,
  repository, branch, PR, agent profile, and recommendation ID.

Legacy attributes such as `claude.session_id`, `claude.message_model`,
`claude.tool_names`, `claude.error`, `claude.tokens_input`, and
`claude.tokens_output` continue to work. Missing token data remains labeled as
estimated. Unknown models retain usage but have `pricing_status=unknown`.

## Governed work context

Use the packaged launcher to attach low-cardinality business identifiers to
Claude's OTel resource attributes:

```bash
berserk-claude --team platform --portfolio ENG --project OBS \
  --feature OBS-142 --work-item ADO-912 --cost-center CC-40 \
  --repository berserk-mcp --branch feature/OBS-142 \
  --agent-profile claude-code --harness-version finops-v1 -- claude
```

The launcher preserves existing `OTEL_RESOURCE_ATTRIBUTES`, enables Claude
telemetry, validates identifiers, and then replaces itself with the requested
command. Agent SDK deployments can set the same attributes per invocation.

Attribution precedence is explicit feature/work item, session work context,
PR mapping, branch mapping, unambiguous repository/project mapping, then
`unattributed`. A session is never silently split across features. Every
financial report exposes attribution coverage and unattributed cost.

## Feature and developer-effort imports

Import CSV, JSON, or NDJSON through a platform-neutral contract:

```bash
berserk-mcp --import-business-data feature --input /absolute/path/features.csv
berserk-mcp --import-business-data effort --input /absolute/path/worklogs.ndjson \
  --input-format ndjson
```

Feature rows require `feature_id`; effort rows require `worklog_id` and
nonnegative `actual_hours`. Include stable source record and update fields so
reimports replace stale versions deterministically. The private local store is
atomic and current-user-only (`0600`/`0700` on POSIX, a protected DACL on
Windows). Existing parent-directory permissions are never changed. When
`BERSERK_MCP_OTLP_LOGS_ENDPOINT` is set, sanitized records are also emitted as
`engineering-work` logs. Remote cleartext HTTP and redirects are refused;
responses and configured headers are bounded and validated.

Hours come only from the effort feed. Session elapsed time, Claude active time,
commits, and lines changed are kept as separate delivery signals and are never
converted into developer hours.

## Tool groups

The original session tools remain available: `claude_recent`,
`claude_sessions`, `claude_tools`, `claude_errors`, `claude_search`,
`claude_loop_check`, `claude_model_fit`, `claude_token_burn`,
`claude_cost_report`, `claude_session_deep_dive`, and
`claude_workflow_insights`. `claude_search` rejects KQL metacharacters.

The enterprise reporting tools are:

| Tool | Purpose |
|---|---|
| `claude_spend_overview` | Group spend and token classes by day, team, project, repository, feature, agent, harness, or model. |
| `claude_feature_cost` | Compare one feature's planned/actual hours and planned/actual/forecast AI spend. |
| `claude_project_economics` | Roll up project features, repositories, and unattributed usage. |
| `claude_efficiency_insights` | Surface expensive operations, poor cache use, retries, loops, model mismatch, and fan-out. |
| `claude_harness_recommendations` | Return deterministic amendment IDs, evidence, confidence, risk, validation window, and rollback criteria. |
| `claude_record_recommendation_decision` | Append an approved/rejected/deferred audit decision; owner and rationale are persisted as hashes. |
| `claude_optimization_impact` | Compare matched model/operation cohorts across immutable harness versions. |
| `claude_management_report` | Return readable Markdown and schema-versioned JSON for portfolio, project, or feature scope. |
| `claude_generate_dashboard` | Write aggregate Markdown or self-contained HTML beneath `BERSERK_MCP_REPORT_DIR`. |

All new analytical tools return a human-readable summary and a JSON envelope
with schema, pricing, attribution, exact/estimated, and freshness metadata.

## Cost semantics

`pricing_catalog.json` is versioned and effective-dated. It prices model
aliases, base and long-context input/output, prompt-cache classes, and
chargeable server tools. Supported provider fast-mode rates are kept separate
from standard speed so mixed cohorts cannot silently inherit the wrong rate.
Reports keep these fields separate:

- `reported_cost_usd`: Claude's approximate emitted signal;
- `public_api_equivalent_usd`: catalog calculation;
- `allocated_license_cost_usd`: reserved for an approved seat-allocation
  adapter;
- `provider_billed_cost_usd`: reserved for billing reconciliation;
- `pricing_status`: `priced`, `partially_priced`, or `unknown`.

Public API-equivalent cost is planning and comparison data, not a provider
invoice. Developer hours and AI dollars are shown side by side and are not
combined unless finance supplies an approved loaded labor rate.

## Headless reports, dashboards, and BI

```bash
berserk-mcp --agent-report --since "6h ago"
berserk-mcp --agent-report --agent-report-mode daily --agent-report-json \
  --since "24h ago"
berserk-mcp --agent-report --agent-report-mode weekly --agent-report-json \
  --since "7d ago"

berserk-mcp --generate-dashboard feature --identifier OBS-142 \
  --since "90d ago" --dashboard-format html
berserk-mcp --export-bi --since "90d ago" \
  --output /absolute/path/ai-finops-export --export-format ndjson
```

Generated HTML uses inline SVG and no external JavaScript or CDN. The BI
export atomically publishes seven stable datasets plus checksums. Grafana JSON
and copy-ready Berserk Explore KQL live in the
[dashboard package](../dashboards/README.md).

Dashboards and BI files are publication outputs. berserk-mcp does not tighten
an existing output directory's mode or ACL; the operator must grant the
management or BI service account the required read access.

## Bounded-query policy

Claude records share the `default` table with other telemetry. FinOps queries
filter by service and time first, summarize inside Berserk, project only the
canonical fields, and cap returned groups. The MCP's existing validation,
query budget, cooldown, concurrency, and read-cache controls still apply. Use
metrics or exported snapshots for long-range reporting rather than repeated
raw multi-month session scans. See the
[KQL performance guide](kql-performance-guide.md).

## Phase J live-verification checklist (live-verified 2026-07-22)

`claude_cost_report`, `claude_session_deep_dive`, and
`claude_workflow_insights` (added 2026-07-20) were unit-tested against
stubbed telemetry and have now been run against the live `homelab`
Berserk instance. Findings:

1. **Attribute presence, 7d window** (11,134 `claude-code` events):
   `claude.session_id` 11,130/11,134 (99.96%), `claude.type` 11,134/11,134
   (100%), `claude.tokens_input`/`claude.tokens_output` 5,313/11,134 each
   (47.7% — the rest fall back to the body-length estimate, exactly as
   designed), `claude.tool_names` 2,611/11,134 (23.4%, only present on
   assistant turns that called a tool, as expected). **File-target
   attributes are absent entirely**: `claude.tool_input.file_path` and
   `claude.file_target` were both 0/1,779 in a 24h sample — this
   forwarder does not currently emit either candidate.
2. **Per-tool live run** (2026-07-22, `homelab` endpoint):
   - `claude_cost_report(since="7d ago", group_by="day")` — **76.8s**,
     real output: `verdict=burn-growing (slope +26.1%/day)`, 8 daily
     buckets with exact/estimated token labels, event and error counts
     per day.
   - `claude_cost_report(since="24h ago", group_by="project")` —
     29.9s, output: `(unattributed): ~3721269 tokens across 1793
     events` — see finding 3.
   - `claude_session_deep_dive("1775e12f-d0ea-4edd-a690-0578e90d5efe",
     since="7d ago")` — 14.4s, output:
     `loop=healthy, ~315 tokens (estimated)` with one contiguous
     no-tool phase example.
   - `claude_workflow_insights(since="24h ago")` — 110.0s (real tool
     sequences, e.g. `Bash→Bash x102`; `Error hotspots: none`;
     top-decile burn target identified). **`since="7d ago"` (this
     tool's registered default) timed out at 120s and did not
     complete** — see finding 4.
3. **Per-project attribution gating: confirmed correct.** With zero
   file-target signal present (finding 1), `claude_cost_report
   group_by=project` correctly stays `(unattributed)`-only rather than
   guessing — the gating logic behaves exactly as designed on this
   deployment's real (attribute-sparse) data.
4. **Timeout finding — action needed.** `claude_cost_report`'s 7d
   `bin(timestamp, 1d)` aggregation completes in ~77s, comfortably
   under the 120s default `BZRK_TIMEOUT` — no change needed there.
   **`claude_workflow_insights` is a different story: its own
   registered default window (7d) reliably times out at 120s on this
   real deployment.** Narrower windows are not uniformly faster,
   either — `24h ago` (1,778 events, no cap hit) took 110.0s, while
   `2d ago` and `3d ago` (both capped at the same 2,000-event ceiling)
   took only 25.8s and 52.7s. This suggests the most recent ~24h of
   data sits in a less-optimized "hot" partition that scans slower
   per-row than the older, settled data the wider windows mostly read
   from before hitting the event cap. **Recommended follow-up:**
   narrow `claude_workflow_insights`'s default `since` from `7d ago` to
   something in the `2d ago`–`3d ago` range (both returned complete,
   meaningful tool-sequence and burn data well within budget), or
   raise `BZRK_TIMEOUT` specifically for this tool. Not yet changed in
   code — flagging here per the checklist's own instructions pending a
   decision on which fix to take.

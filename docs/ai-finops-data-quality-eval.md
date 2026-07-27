# AI FinOps data-quality eval

Use this eval to separate "the AI FinOps feature is broken" from "the cluster
does not yet contain enough telemetry or business metadata for useful FinOps
answers."

The eval is read-only. It calls Claude and FinOps reporting tools and scores
whether the returned data includes the fields needed for management reporting.

## Command-line eval

From the repository checkout:

```bash
BERSERK_MCP_ROLE=claude python3 evals/ai_finops_data_quality.py \
  --json-out /tmp/berserk-mcp-ai-finops-dq.json
```

To test an installed command:

```bash
BERSERK_MCP_ROLE=claude python3 evals/ai_finops_data_quality.py \
  --server-command berserk-mcp \
  --json-out /tmp/berserk-mcp-ai-finops-dq.json
```

Expected signal coverage:

- exact token fields or a clear estimated-token fallback
- message-level de-duplication evidence
- pricing catalog coverage and unpriced-model warnings
- project/repository/file attribution coverage
- feature/work-item metadata coverage
- agent profile and harness version coverage
- cache token coverage where available
- freshness and missing-data warnings
- owner pseudonym/privacy language

Interpretation:

- `good`: enough telemetry exists for management reporting
- `partial`: feature works, but ingestion/business metadata is incomplete
- `poor`: mostly missing data; improve telemetry mapping before judging feature
- `fail`: at least one MCP tool errored or returned a likely regression marker

## Instructions for Claude

Give Claude this prompt:

```text
Run the AI FinOps data-quality eval.

Use:
BERSERK_MCP_ROLE=claude python3 evals/ai_finops_data_quality.py \
  --json-out /tmp/berserk-mcp-ai-finops-dq.json

Then read the JSON and summarize:
- verdict and signal score
- missing signals
- failed tool calls
- whether exact token usage is available or estimated
- pricing/model coverage
- project/feature/harness coverage
- what ingestion or business metadata should be improved next

Do not treat missing data as a code bug unless a tool returns an MCP error,
traceback, unsafe raw value, or contradictory accounting result.
```

## Manual fallback

If scripts cannot run, ask Claude to call these tools manually:

```text
Run:
1. claude_token_burn since="30d ago"
2. claude_cost_report since="30d ago" group_by="day"
3. claude_cost_report since="30d ago" group_by="model"
4. claude_cost_report since="30d ago" group_by="project"
5. claude_spend_overview since="30d ago" group_by="day"
6. claude_spend_overview since="30d ago" group_by="project"
7. claude_spend_overview since="30d ago" group_by="model"
8. claude_efficiency_insights since="30d ago"
9. claude_harness_recommendations since="30d ago"
10. claude_management_report scope="portfolio" since="30d ago"

Score the same signals listed in docs/ai-finops-data-quality-eval.md.
```

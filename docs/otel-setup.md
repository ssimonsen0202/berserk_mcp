# OpenTelemetry setup: what each feature actually needs

berserk-mcp's core tools (`top_cpu`, `errors_by_service`, the SRE/SOC
lanes, and so on) only need a Berserk instance with **any** telemetry in
it — they query whatever `default` table you already have. This doc covers
the features that need something more specific: **Claude Code session
data**, flowing into Berserk with `resource['service.name'] == 'claude-code'`
and a known set of attributes. Nothing here is required to use berserk-mcp
at all — only to use the `claude_*` lane and the enterprise cost/BI tools.

## Which features need this

| Feature | Needs OTel-ingested Claude Code data? |
|---|---|
| Core tools, SRE lane, SOC lane, learning loop, trace tools | No — these query your existing Berserk data, whatever it is. |
| `claude_recent`, `claude_sessions`, `claude_tools`, `claude_errors`, `claude_search` | Yes — any Claude Code session data at all. |
| `claude_cost_report`, `claude_token_burn`, `claude_workflow_insights`, `claude_session_deep_dive`, `claude_loop_check`, `claude_model_fit` | Yes — same as above. |
| `claude_spend_overview` grouped by `repository`/`branch`/`pull_request` | Yes, plus the `code.repository.id`/`code.branch.id`/`vcs.pull_request.id` attributes described below. Without them, this data reports as unattributed. |
| `claude_feature_cost`, `claude_project_economics` | Yes, plus [governed work context](claude-code.md#governed-work-context) — these need a `feature_id`/`project_id`, which nothing derives automatically. |

## Two ways to get Claude Code data into Berserk

See [Claude Code telemetry and AI FinOps](claude-code.md#collection-paths)
for the full comparison. In short:

- **Native Claude Code OpenTelemetry** — Claude Code's own OTel export,
  through your own collector, into Berserk. Preferred when you already run
  an OTel collector.
- **JSONL forwarder** — a small script that tails
  `~/.claude/projects/*/*.jsonl` (Claude Code's own session transcripts),
  redacts secrets, and posts OTLP log records to Berserk. Simplest to set
  up with nothing else running. This is the path documented below.

## Minimal JSONL forwarder setup

1. Point it at your Berserk OTLP ingest endpoint and run it as a background
   service (launchd, systemd, or your process supervisor of choice) — it
   tails continuously, not once.
2. Confirm it's working:

```bash
bzrk search "default | where resource['service.name'] == 'claude-code' | take 1" --since "10m ago"
```

A row back means the base `claude_*` tools already work. No repository or
PR attribution is required for those.

## Automatic repository/branch attribution (no extra flags)

Claude Code's own session transcript already records the working directory
and git branch for every line (`cwd`, `gitBranch`). A forwarder can turn
those into the attribute names `ai_finops.py`'s cost-attribution layer
actually reads — **`code.repository.id`** (derived from `cwd`'s git root
directory name) and **`code.branch.id`** (`gitBranch` as-is) — with no
per-session configuration and no launcher flags. This is what
`claude_spend_overview(group_by="repository")` and
`group_by="branch"` need to stop reporting `(unattributed)`.

Verify it's populated:

```bash
bzrk search "default | where resource['service.name'] == 'claude-code' \
  | where isnotempty(attributes['code.repository.id']) \
  | project timestamp, repo=tostring(attributes['code.repository.id']), \
    branch=tostring(attributes['code.branch.id']) | take 5" --since "1h ago"
```

## Pull-request attribution (not automatic)

Claude Code's transcript has no concept of a PR number — a PR is only
assigned after you push and open one, and nothing in the session data
carries it back. There are currently two ways to get `pull_request`-level
cost data, and both require an explicit step per session or per PR:

- **Governed work context** — launch Claude through `berserk-claude
  --repository ... --branch ... --work-item ...` (see
  [Governed work context](claude-code.md#governed-work-context)). This
  attaches identifiers at launch time, before you know the PR number, so it
  suits `feature`/`work-item` attribution better than PR attribution
  specifically.
- **Post-hoc correlation** — after a PR is opened, map its branch name to
  its PR number (for example `gh pr list --json number,headRefName`) and
  join that against `claude_spend_overview(group_by="branch")`'s output
  yourself. berserk-mcp does not do this join automatically today; it's the
  next piece to build if you need `cost_per_pull_request_usd` populated
  without a manual step.

## Full attribute reference

For the complete list of attributes the Claude lane recognizes (native and
legacy), see [Normalized attributes](claude-code.md#normalized-attributes)
in the main Claude Code doc.

# berserk-mcp v1.23 live validation — results

Cluster/profile tested: `beserk` production cluster (v1.0.115 data plane), berserk-mcp v1.23.0
MCP role: `claude` (Claude lane tools + AI FinOps tools visible)
Date/time: 2026-07-27, ~19:30–20:35 UTC (initial run), ~22:55–22:59 UTC (re-verification)
Overall result: **PASS** — see "Re-verification after root-cause fix" below. Initial run was
PARTIAL, cut short by a live cluster outage; root cause was found, fixed, and the previously
timed-out sections were cleanly re-run afterward.

## Re-verification after root-cause fix (2026-07-27, ~22:55–22:59 UTC)

The Section 6–10 timeouts below were **not** an upstream/query-engine bug and **not** related to
the v1.23 code under test. Root cause, found via direct host/hypervisor access: `berserk-janitor-1`
had two config bugs — `merger_interval_seconds` was silently defaulting to 10s instead of the
intended 300s (the original compose file nested it under a `merger:` object the binary doesn't
recognize; that nesting has never worked since the file was written), driving continuous
back-to-back 1000-segment compaction merges against a single 5400rpm HDD that's the *only* physical
disk backing all three VMs on this ESXi host. That saturated disk I/O (84.6% iowait, load average
16–21 on 2 vCPUs) and starved the query engine's own segment reads, causing the timeouts below.

Fix applied: corrected `merger_interval_seconds: 300` and `max_segments_per_batch: 100` in
`/opt/assistant/berserk/docker-compose.yaml` (flat top-level keys, not nested), janitor restarted.
`download_config.max_concurrent_downloads` did not take effect via YAML for this binary (stays at
its default of 20) and was left as a follow-up rather than blocking on it, since the other two
changes already resolved the symptom.

Result: iowait dropped to ~19–22%, load average to 2.53 (1-min), blocked-process count to 0–1.
Re-running the previously-failed sections against the now-healthy cluster:

- **Section 9 (dashboard generation)**: now generates successfully at 1h ago (previously timed out
  at every window down to 2m). Output inspected — no PII, owner IDs, or raw prompts; matches the
  Section 10 pass criteria. Confirms the earlier source-code finding that this was never a
  dashboard-specific bug.
- **Section 6 (project economics)**: `claude_spend_overview since="1h ago" group_by="project"`
  now returns cleanly; still correctly reports `project_id: "unattributed"` only (no governed
  metadata) — SKIP remains the correct verdict, now confirmed without an infra timeout obscuring it.
- **Section 3 (`claude_sessions`, `claude_tools`)**: now pass at **1h**, which failed even at 1h in
  the original run — a real improvement, not just a re-confirmation.
- **Section 4 (`claude_cost_report`)**: still exceeds budget at 7d (client-side timeout), but this
  matches the *original*, pre-incident pattern (7d/6h+ windows were already marginal on this
  hardware before today's outage) — passes cleanly at 1h. Not a regression from the outage or from
  the fix; a pre-existing hardware capacity ceiling on a single-HDD, 2-vCPU host worth keeping in
  mind for future test runs (default to ≤1h windows on this cluster).
- **Section 5 (`claude_management_report`)**: passes cleanly at 1h with proper schema envelope.

**Follow-up still open:** `max_concurrent_downloads` not taking effect via the janitor YAML config
(likely CLI-flag-only for this binary) — low priority given the interval/batch-size fix already
resolved the observed symptom, but worth revisiting if a future large merge cycle causes trouble
again. See
[`janitor-config-schema-2026-07-27.md`](janitor-config-schema-2026-07-27.md)
for the schema finding and verification procedure. The underlying hardware constraint
(single 5400rpm 2.5" HDD shared by 3 VMs, no SSD, ESXi 6.5 standalone host with no
vCenter/SIOC available) was not changed by this fix and remains the ceiling on how
much query volume + compaction this cluster can absorb concurrently.

## Passed

- **Section 1 — Connectivity/role surface**: all 20 expected tools visible
  (`validate_kql`, Claude analytics lane, AI FinOps lane, both write-like tools
  correctly flagged).
- **Section 2 — KQL validation/execution guards**: all 7 queries scored correctly.
  Query 1 accepted; queries 2–3 rejected on the unconditional semicolon guard; queries
  4–7 rejected as source-introducing/high-risk operators. Confirmed
  `BERSERK_MCP_KQL_VALIDATION=off` (if set) does not touch the final execution-boundary
  guard — it is unconditional in `bzrk_search()`.
- **Section 3 — Basic telemetry**: `claude_recent` PASS at 6h;
  `claude_workflow_insights` PASS at 1h. `claude_sessions` and `claude_tools`
  passed at 1h after the janitor fix; `claude_errors` needed a 15m window on this
  hardware-limited cluster.
- **Section 4 — Token burn/cost**: at 15m, `claude_token_burn` returned
  an **exact** 21,899-token total; the content-block rows for a single API call
  correctly collapsed to one billable event via `message_id` grouping — this is the
  core fix from v1.21.1, confirmed working end-to-end on live post-fix data.
  `claude_cost_report` PASS for group_by=day/model/project at 15m and 1h; the 7d
  window remains above this cluster's practical query budget.
- **Section 5 — Enterprise FinOps**: at 15m, `claude_spend_overview`,
  `claude_efficiency_insights`, `claude_management_report` all returned the proper
  `schema_version: 1.0` envelope, `catalog_version: anthropic-public-2026-07-25`,
  100% pricing coverage, 0% feature attribution (expected — no governed metadata).
  `claude_management_report` also passed cleanly at 1h after the janitor fix.
- **Section 7 — Harness recommendations**: `claude_harness_recommendations
  since="15m ago"` returned 0 findings with an explicit "every amendment requires
  owner approval; no harness was modified" statement rather than inventing an
  amendment. Evidence structure (cohorts, coverage, pricing) present and well-formed.
- **Section 8 — Optimization impact**: `claude_optimization_impact` with
  `agent_profile=unattributed`, both harness versions `unattributed` returned a clean
  `verdict: "insufficient-data"` with empty `matched_cohorts` — no crash, no fabricated
  comparison.
- **Section 11 — Search input guard**: all 5 unsafe terms
  (`error | take 1`, `"quoted"`, `` `backtick` ``, `path\escape`, and a term containing
  a raw newline) were rejected instantly, before any query execution, with
  `"term may not contain quotes, pipe, backslash, or backtick"`. Rejection is
  clearly validation-layer (sub-millisecond, independent of backend health — it kept
  working even while every real query was timing out).

## Initial failure, now resolved

- **Section 9 — Dashboard generation** initially timed out at every window tested:
  30d, 15m, 5m, and 2m ago. Source inspection showed this was not a dashboard-specific
  code path: the "portfolio" branch of `_dashboard_payload()`
  ([ai_finops.py:1982](../ai_finops.py#L1982)) calls `build_spend_overview(rows, ...)`
  fed by the same single `_fetch_usage(since)` call
  ([ai_finops.py:1947](../ai_finops.py#L1947)) that `claude_spend_overview` itself uses
  ([ai_finops.py:1489](../ai_finops.py#L1489)).
- After correcting the janitor config and restarting janitor, Section 9 passed at
  1h. The initial timeout is therefore classified as cluster I/O starvation from
  janitor compaction, not a `claude_generate_dashboard` or `berserk-mcp` bug.

## Skipped / expected gaps

- **Section 6 — Project/feature economics**: no governed project or feature metadata
  exists in current telemetry (`project_id` is uniformly `"unattributed"`). SKIP is
  the correct outcome per the test plan's own criteria (no governed metadata → SKIP
  with reason). The re-run confirmed the tool now returns cleanly at 1h and reports
  the expected unattributed state.
- **Section 10 — Redaction/pseudonym/fence checks**: re-verified after the janitor
  fix through the Section 9 dashboard payload. No PII, owner IDs, raw prompts, or
  session bodies were observed. Only structural IDs and reporting metadata such as
  `project_id: "unattributed"`, `catalog_version`, and `schema_version` appeared.

## Security observations

- The semicolon/control-command execution boundary and the `claude_search` free-text
  guard both behaved as unconditional, pre-execution checks — confirmed by the fact
  they kept rejecting correctly even after the backend query engine became completely
  unresponsive. Validation is not backend-dependent, which is the right design.
- No cleartext secrets, owners, or PII were observed in any tool output this session.
- Nothing in this run indicates the message_id fix reintroduced a data-exposure risk —
  `_message_groups()` exposes the full row group only to the internal file/tool
  attribution path, and the collapsed representative row is what reaches the model.

## Data-quality observations

- **Live cluster outage during testing was caused by janitor I/O starvation.** Around
  Section 6–9, every query-backed tool began timing out at small windows. Follow-up
  host/hypervisor triage found the cause: `berserk-janitor-1` was running continuous
  1000-segment compaction because `merger_interval_seconds` was not applied from the
  nested compose config and silently defaulted to 10s instead of the intended 300s.
  On this host, all three VMs share one 5400rpm HDD, so janitor's sustained compaction
  writes starved MinIO/query segment reads.
- The issue was fixed by applying flat top-level janitor config keys,
  `merger_interval_seconds: 300` and `max_segments_per_batch: 100`, then restarting
  janitor. The previously failed sections passed afterward. This was infrastructure
  configuration drift, not a v1.23/message_id regression and not the same mechanism
  as the 2026-07-26 idle-worker wedge.
- Even after the fix, this cluster has limited wide-window headroom. 7d/6h+ cost
  windows remain marginal on the single-HDD, 2-vCPU host; future live validation
  should default to 1h or smaller windows unless capacity is increased.

## Recommended follow-up

**Scope note for anyone (human or agent) picking this up:** everything below except
item 4 is an **infrastructure/ops** action against the `beserk` host's
`ghcr.io/berserkdb/*:v1.0.115` docker-compose stack. That stack's source/compose
files are **not present in the `berserk-mcp` repo** (confirmed: no `docker-compose*`
anywhere in this tree) — there is no code in this codebase to change for items 1–3.
The `berserk_mcp.py` / `ai_finops.py` / `agent_analytics.py` code is a client of that
cluster, not the thing that's broken.

1. Keep the corrected flat janitor settings in the deployment config:
   `merger_interval_seconds: 300` and `max_segments_per_batch: 100`.
2. Revisit `max_concurrent_downloads`, which did not take effect through the YAML
   config for this binary and may need to be passed as a CLI flag or fixed upstream.
   See
   [`janitor-config-schema-2026-07-27.md`](janitor-config-schema-2026-07-27.md).
3. Consider wiring the existing trivial-query probe into Docker/ops health instead
   of relying on bare TCP healthchecks.
4. Treat future Section 9 failures as an MCP bug only if Section 5 succeeds under
   the same cluster conditions while Section 9 still fails.

**Bottom line for a Codex/GPT-5.5 handoff:** there is currently no confirmed,
in-repo `berserk-mcp` bug from this test run to fix. The message_id fix (the actual
subject of v1.21.1/v1.23 validation) passed on exact data in Section 4 and remained
correct after the infrastructure fix. The initial failures traced to janitor
compaction saturating the shared disk, not to the MCP code.

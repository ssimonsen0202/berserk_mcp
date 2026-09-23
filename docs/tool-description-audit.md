# Tool-description audit (issue #77)

A structured pass over tool descriptions using the 6-axis rubric from
["MCP Tool Descriptions Are Smelly!"](https://arxiv.org/html/2602.14878v1)
(arXiv 2602.14878) — Purpose, Usage Guidelines, Limitations, Parameter
Explanation, Length/Completeness, Examples — applied by hand rather than
that paper's automated LLM-jury scanner, given this project's small, stable
tool count. Every prior fix this project has made to a tool description
came from a real eval miss found after the fact; this audit is the
proactive counterpart: find gaps before a real model does.

## Scope

Per this issue's own priority, the SRE lane (10 tools) and Claude lane (21
tools) — 31 of the ~70 registered tools, the two lanes with the most
existing eval coverage and the easiest to verify a fix against. The
remaining lanes (SOC, core, discovery, learning-loop, parser factory,
CanonLoom) are unaudited; a follow-up issue can pick those up using the
same method.

## What the rubric found

Most tools in both lanes already clear the rubric: a clear Purpose sentence,
concrete "Use for 'X'" Guideline phrasing, and, where relevant, an explicit
Limitations statement (`claude_loop_check`'s "output is diagnostic, not raw
transcript replay", `claude_harness_recommendations`'s "never modifies a
harness", `claude_generate_dashboard`'s "explicit local write",
`claude_quota_status`'s macOS-only/undocumented-endpoint caveat). This
project's existing convention of grounding descriptions in real "use for"
phrasings is doing real work.

Two real, actionable gaps found and fixed:

### 1. `sre_error_rate` had no cross-reference to `investigate_error_rate`

`investigate_error_rate`'s description already disambiguates from
`sre_error_rate` ("not just a rate check — see sre_error_rate for that"),
added in the earlier description fix (docs/model-routing-cost-validation-2026-08-23.md's
2026-08-29 addendum). That fix was one-directional. `sre_error_rate` never
said the reverse. Given the still-open residual case from that same
addendum (`investigate_error_root_cause_2`, which keeps routing to
`sre_error_rate` on `mistral-saba` instead of `investigate_error_rate`),
adding the symmetric cross-reference was worth testing directly against
that exact case.

**Result: the case still fails on `mistral-saba` after this fix.** Honest
finding, not a fix that worked — this phrasing ("errors jumped... causing
it") remains genuinely hard for that model even with disambiguation in both
directions. Re-verified the fix didn't cause a *new* regression by adding a
guard-rail case (`sre_error_rate_guardrail`: "Is the error rate for
checkout climbing right now?") — confirmed `sre_error_rate`'s own territory
wasn't hijacked by `investigate_error_rate`'s broadened description, on
both `mistral-saba` and `deepseek-v4-flash`. The residual case stays open,
now confirmed harder than a description-symmetry fix can solve — a
candidate for the eventual escalation-policy work (issue #23) rather than
more description tuning.

### 2. `validate_kql` had no Usage Guidelines at all

Every sibling SRE/core tool has a "Use for 'X'" phrase; `validate_kql` was
the one tool in the audited scope with none — just a Purpose sentence and
mode explanation. Added `Use for 'check this query before I save it' or
'will this KQL work'`. No prior eval case exercised this tool, so there's
no before/after routing-accuracy number for this one; it's a
rubric-driven fix without a specific failure to compare against, unlike
the changes in the routing-validation doc's addenda.

## Pattern noted, not fixed here

Several `claude_*` tools (`claude_recent`, `claude_sessions`,
`claude_tools`, `claude_errors`, `claude_search`, `claude_spend_overview`,
`claude_feature_cost`, `claude_project_economics`,
`claude_efficiency_insights`, `claude_management_report`) skip the "Use
for" phrasing pattern — they describe their output shape instead. All of
these have existing eval coverage and route correctly in every real-model
run so far (see docs/model-routing-cost-validation-2026-08-23.md). Per
this project's own discipline — verify before changing, don't fix what
isn't broken, avoid speculative description churn without a failing case
to test against — these are left alone. Worth revisiting only if one of
them shows up as a real miss in a future eval run.

## Verification

- Full test suite: 914 tests, unaffected.
- `ci_gate.py`: unaffected by the description changes; the new
  `sre_error_rate_guardrail` case (with a matching mock-router branch)
  keeps the mock baseline accurate.
- Real-model verification against `mistral-saba` and `deepseek-v4-flash`,
  full case set, before and after both fixes — see
  docs/model-routing-cost-validation-2026-08-23.md for the routing-accuracy
  side of this; this doc is the audit methodology and findings, that one is
  the eval data of record.

## Addendum, 2026-09-03: proactive cluster collision analysis (Task 2)

Every fix above, and every fix in `docs/model-routing-cost-validation-2026-08-23.md`,
was reactive — found only after a real eval miss exposed it. This addendum is
the proactive counterpart: `evals/tool_collisions.py` ranks tools whose
descriptions or names are lexically close enough that a model could plausibly
confuse them, *before* a real miss happens. See the module docstring for full
method; summary here.

**Two independent collision mechanisms, not one.** An early version scored
only description-vs-description similarity and missed two of three known
ground-truth collisions. Both misses turned out to share the same cause: a
**shared word in the tool's NAME** (`tool_discovery.search()` weights a
name-token match 3x a description match), not the description text. Adding
`name_token_edges()` — flag any content word shared by 2-3 tool names, above
that treated as an intentional lane prefix (`claude` shared by 21 tools,
`sre`/`soc`/`canonloom` by 5 each) — found both:

- `claude_search` / `search`, sharing the name-token `search` — the exact
  cross-lane pair behind `mistral-small-3.2-24b-instruct`'s
  `role=all` → `role=claude` accuracy jump (80.4% → 89.5%,
  `evals/run_ledger.jsonl` 2026-09-03T19:46/19:48).
- `claude_workflow_insights` / `claude_efficiency_insights`, sharing
  `insight` — the third competitor that moved into the gap after the
  2026-09-03 reciprocal fix (`jworkflow_burn` now loses to
  `claude_efficiency_insights`, not `claude_token_burn`).

**Acceptance check against the three known ground-truth collisions**
(`docs/task-brief-collision-clusters-2026-09-03.md`):

1. `claude_workflow_insights` / `claude_token_burn` / `claude_errors` /
   `claude_efficiency_insights` all one cluster — **yes**.
2. `claude_session_deep_dive` / `claude_loop_check` same cluster — **no when
   this was first checked, documented as a miss** for the same reason as
   above: no shared name token, description-ratio too weak (~0.25-0.30) to
   separate from noise without flooding the report. **Since resolved as a
   side effect of Task 1's fix**, not by improving this method: Task 1 added
   "see claude_session_deep_dive instead" to `claude_loop_check`'s
   description, which gave the two tools enough shared vocabulary that this
   method now finds the pair too (`tests/test_tool_collisions.py`'s
   `test_session_deep_dive_loop_check_now_found`). Kept as the worked
   example in the module docstring — this method finds most lexical
   collisions, not all, and a real eval remains the ground truth that
   catches everything, including retroactively confirming a fix by making a
   collision newly visible.
3. `claude_search` / `search`, isolated as a clean 2-tool cluster with no
   other members — **yes**.

**Known over-firing on SRE/SOC.** The script includes a sanity guard: SRE and
SOC measured 95-96% accuracy even at the full 74-tool schema
(`evals/run_ledger.jsonl`), so a role-scoped report flagging many clusters
there is the method over-firing, not finding real risk. It does: `--role sre`
flags 7 clusters, `--role soc` flags 6, both past the guard's threshold of 3.
**Do not act on SRE/SOC clusters from this tool without a real eval miss to
confirm one first.**

**Ranked output, unscoped and `role=claude`:** saved for reference at
`/tmp/collisions_full.txt` and `/tmp/collisions_claude.txt` when this addendum
was written (not committed — regenerate with `python3 evals/tool_collisions.py
[--role claude]`, deterministic, no API calls). The `claude`-lane cluster
above (`[4]` in that output) is the direct input to Task 1's fix.

**Reminder from the module docstring, worth restating here too:** this is a
candidate generator, not a verdict. A flagged cluster still needs a real eval
case before a description gets touched — that discipline is what keeps this
proactive pass from becoming the speculative churn the rest of this document
already warns against.

## Addendum, 2026-09-22: SOC spike and new-services descriptions

**Changed:** `soc_log_spike`, `detect_anomalies`, `soc_new_services`,
`detect_new_sources` (commit `85f14ef`).

**Reason: accuracy, not a routing miss.** Two descriptions claimed behaviour
their queries do not have:

- `soc_log_spike` claimed "largest log volume" and "anything anomalous".
  `Q_SOC_LOG_SPIKE` returns a raw per-minute count series for each service. It
  does no ranking and no statistics; `detect_anomalies` does the statistics.
- `soc_new_services` claimed "did a new source appear". `Q_SOC_NEW_SERVICES`
  takes `min(timestamp)` inside the query window, so a quiet long-lived
  service looks new. `detect_new_sources` compares against a stored baseline
  (`parser_factory.detect_new_sources`; the first run only records it).

Each description now states what its query does and names its pair.

**Evidence.** Two cases use the phrases both tools claimed:
`nm_anything_anomalous` and `nm_did_new_source_appear`
(`evals/router_cases_nearmiss.jsonl`). `nm_volume_not_statistical` and
`nm_first_seen_order` are the guard rails for the other side of each pair.
Runs are in `evals/run_ledger.jsonl`, 2026-09-22.

| Model | Before (`e2f1ca4`) | After (`85f14ef`) | The 4 cases |
|---|---|---|---|
| Haiku 4.5 | 39/40 | 39/40 | 4/4 before and after |
| DeepSeek V4.1 Flash | 40/40 | 40/40 | 4/4 before and after |

Both models routed the shared phrases correctly before the change. The change
fixed no routing miss and caused no regression. Haiku's one miss is the same in
both runs (`nm_drain_not_generate_one` → `discovery_status`) and is not related
to these tools.

**Side effects to know about:**

- `evals/tool_collisions.py` now joins `soc_log_spike` and `detect_anomalies`
  (description ratio 0.57). The cross-references add shared words. This is the
  same effect as the `claude_loop_check` cross-reference in the addendum above.
  It does not mean the pair is more confusable.
- `detect_anomalies` has one frozen canary case (`detect_anomalies_volume`),
  and the canary does not fingerprint the tool schema. A small canary score
  change after `85f14ef` comes from this description change, not model drift.

## Addendum, 2026-09-23: discovery queue status vs worker

**The miss.** Haiku 4.5 routed `nm_drain_not_generate_one` ("Work through
everything waiting in the discovery queue.") to `discovery_status` instead of
`run_discovery_worker` in both near-miss runs on 2026-09-22 (39/40 each;
`evals/run_ledger.jsonl`). DeepSeek V4.1 Flash routed it correctly both times.

**Changed.** `discovery_status` now says it is read-only and processes nothing;
`run_discovery_worker` says it processes up to `max_jobs` pending jobs (default
1, maximum 5). Each names the other. The rewrite avoids the eval prompt's own
words. Guard rail added: `nm_queue_status_not_drain` ("Just show me what is
sitting in the discovery queue. Don't process anything." → `discovery_status`).

**Result: the miss remains.** After-run 2026-09-23T20:22 (Haiku 4.5, 41
cases): 40/41, the same single miss, and the guard rail passes, so no
regression. That run used the first rewrite ("drain the pending jobs"); a review
then corrected it to the `max_jobs` wording, which changes no routing cue.

**Reading.** Kept for accuracy, not as a routing fix. Haiku appears to check
the queue before starting a slow, LLM-backed worker even when asked to
process it. That is a defensible cautious habit, not a wording gap, like the
`mistral-saba` residual case in the first addendum. Do not tune the
description further for this case without a second model showing the miss.

## Follow-up

The SOC, core, discovery, learning-loop, parser-factory, and CanonLoom
lanes (~39 tools) are unaudited. Whoever picks this up next should order the
work by `evals/tool_collisions.py`'s cluster ranking rather than lane by
lane (noting the SOC over-firing caveat above), use the same 6-axis rubric
and the same discipline: only change a description with either a real eval
miss to fix, or a clear rubric gap (like `validate_kql`'s missing
Guidelines) worth testing proactively — and verify any change against real
models before shipping it, not just by reading the text.

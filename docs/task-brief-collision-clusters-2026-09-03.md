# Task brief: cluster collision analysis, then the `claude`-lane cluster fix

Written 2026-09-03 for execution in a following session. Implementation-grade
on purpose: exact files, signatures, commands, and exit criteria, so the
executing session does not re-derive decisions already made.

**Read first:**
[docs/mistral-small-optimization-plan-2026-09-03.md](mistral-small-optimization-plan-2026-09-03.md)
(Phase 0 results and what they mean) and the 2026-09-03 addendum in
[docs/model-routing-cost-validation-2026-08-23.md](model-routing-cost-validation-2026-08-23.md)
(the two-round description fix and why round 1 failed).

**Evidence rule for this whole brief:** every run appends to
[`evals/run_ledger.jsonl`](../evals/run_ledger.jsonl) automatically. Cite ledger
records, not remembered numbers. Check `case_count` and `role` before quoting
any accuracy -- a `--limit` smoke run and a role-scoped sweep both land there.

---

## Why this work exists

On 2026-09-03 a real cross-model routing bug was found and fixed:
`claude_workflow_insights` was losing prompts to `claude_token_burn` and
`claude_errors`. The fix worked (`mistral-saba` 86.3% → 92.2%,
`deepseek-v4-flash` 90.2% → 94.1%, zero regressions).

Then Phase 0 tested `mistral-small-3.2-24b-instruct` and found the fix had
**not** closed the problem -- a *third* competitor stepped into the gap:

```
jworkflow_burn        expect claude_workflow_insights  got claude_efficiency_insights
jworkflow_burn_2      expect claude_workflow_insights  got claude_efficiency_insights
jworkflow_hotspots_2  expect claude_workflow_insights  got claude_errors
jdive_loop            expect claude_session_deep_dive  got claude_loop_check
```

**The lesson driving both tasks: these are clusters, not pairs.** Knock down one
competitor and the next one takes the call. Task 2 finds clusters
systematically; Task 1 fixes the known one cluster-wide.

---

## Task 2 (do this first) -- cluster collision analysis

Free, offline, no API calls. Do it first because its output defines the
priority order for Task 1's follow-on work and for the ~39 unaudited tools.

**Create:** `evals/tool_collisions.py`

**Approach.** Reuse the existing index; do not build a new matcher.

```python
import tool_discovery as td
import berserk_mcp as bm

index = td.build_index(bm.TOOLS + bm.MGMT_TOOLS)


def self_query_scores(index, tools, top_k=8):
    """Query the index with each tool's OWN description, and record which
    other tools rank near it.

    Returns {tool_name: [(competitor_name, score, ratio_to_self), ...]}
    where ratio_to_self = competitor_score / that tool's own self-score.
    A competitor scoring near 1.0 is retrieved almost as strongly by the
    tool's own words as the tool itself -- that is the collision signal.
    """


def find_clusters(scores, ratio_threshold=0.85):
    """Connected components over the graph where an edge A->B exists when
    B's ratio_to_self under A's query >= ratio_threshold. Returns clusters
    sorted by (size, mean edge weight) descending -- worst first."""


def report(clusters, role=None):
    """Print ranked clusters. When role is given, only include tools visible
    under that role (bm.tool_visible with BERSERK_MCP_ROLE set), so
    cross-lane collisions can be separated from within-lane ones."""
```

**Acceptance test -- this is the real gate, and ground truth exists.** The
script must rediscover, without being told about them:

1. the cluster `claude_workflow_insights` / `claude_token_burn` /
   `claude_errors` / `claude_efficiency_insights`
2. the pair `claude_session_deep_dive` / `claude_loop_check`
3. the cross-lane pair `claude_search` / `search` (only when run unscoped --
   it disappears under `role=claude`, which is exactly why `role=all`
   measured 80% and `role=claude` measured 89%)

If the ranking does not surface all three near the top, the scoring is wrong.
Tune `ratio_threshold` and the query text (full description vs. just the
`Use for` clause) until it does, then leave the tuning documented in the
module docstring.

**Guard against false positives.** The SRE and SOC lanes measured 96% and 95%
-- they are known-good. If the report ranks a pile of SRE tools as severe
collisions, the metric is over-firing. Say so in the output rather than
silently producing a long list.

**Honest caveat to state in the module docstring:** lexical proximity is a
*candidate generator, not a verdict*. A flagged cluster still needs a real eval
case to confirm it is a routing risk. Do not fix on proximity alone --
`docs/tool-description-audit.md` already warns against speculative description
churn.

**Deliverable:** the script, plus its ranked output pasted into a short
addendum on `docs/tool-description-audit.md`.

---

## Task 1 -- fix the `claude`-lane cluster

**Current state (ledger, 2026-09-03T19:48):** `role=claude`, 46 tools, 38
cases, **34/38 = 89.5%**, 4 misses listed above.
**Target:** ~37/38 (~97%). Every other lane is already at 95-100% and needs
nothing.

**Files:** `berserk_mcp.py` (tool descriptions, around lines 2332-2345),
`evals/router_cases.jsonl` (held-out cases).

### The four rules -- each earned from a measured failure

1. **Cluster-wide, not pairwise.** Round 1 on 2026-09-03 broadened only the
   target tool's description and measured *zero* effect on `deepseek-v4-flash`.
   Round 2 added reciprocal "not X -- see Y for that" lines to the *competing*
   tools and worked. But the cluster has 4 members and 12 directed pairs --
   **do not write all 12.** Find the minimal set of disambiguating statements
   that separates the cluster. Bloating four descriptions to fix three cases
   is a net loss.

2. **Compare miss sets, not percentages.** On 2026-09-03 a fix that genuinely
   worked showed *flat* aggregate accuracy, because an unrelated case flipped
   by run-to-run noise and offset it exactly. Reading the percentage alone
   would have concluded "no effect" and reverted a good fix. Always diff the
   miss lists between ledger records.

3. **Zero regressions.** A previously-passing case that starts failing kills
   the change, even if the aggregate improved. Check the full 51-case
   `role=all` run too, not just the `claude` lane -- a description edit is
   global.

4. **Held-out variants.** Add at least one new differently-phrased case per
   tool you touch, plus a guardrail case confirming the *competing* tool still
   wins its own territory. `jtoken_burn_guardrail` is the existing example of
   this pattern.

### Commands

```bash
export OPENROUTER_API_KEY="$(security find-generic-password -s openrouter-api-key -a "$USER" -w)"

# claude lane (the target) -- regenerate the filtered case file if cases changed
BERSERK_MCP_ROLE=claude python3 evals/run_eval.py <lane-cases>.jsonl \
  --backend openai --base-url https://openrouter.ai/api/v1 \
  --key-env OPENROUTER_API_KEY \
  --model "mistralai/mistral-small-3.2-24b-instruct" --tool-choice auto

# regression check, full schema, all cases
python3 evals/run_eval.py evals/router_cases.jsonl \
  --backend openai --base-url https://openrouter.ai/api/v1 \
  --key-env OPENROUTER_API_KEY \
  --model "mistralai/mistral-small-3.2-24b-instruct" --tool-choice auto
```

Confirm on a second model (`deepseek-v4-flash`, cheap and reliable) before
committing. `deepseek-chat` is unavailable -- OpenRouter's shared upstream pool
for it is saturated; do not burn attempts on it.

**Budget:** ~$0.10 per iteration. The OpenRouter key has a **$10/month cap with
~$8.70 remaining**, so this is comfortable but not unlimited.

**Stop condition: cap at 3 iterations.** If the cluster has not converged by
then, stop and write up what was tried and what each attempt measured. That is
the escalation signal. Do not keep tuning -- past three attempts the risk is
overfitting descriptions to these specific prompts, which both
`docs/tool-description-audit.md` and the routing-validation doc explicitly rule
against. A documented "this resists description fixes" result is a legitimate
outcome, matching how `investigate_error_root_cause_2` was handled.

### Definition of done

- `claude` lane improved, zero regressions on the full 51-case run
- Confirmed on a second model
- Held-out variants and a guardrail case added
- New addendum on `docs/model-routing-cost-validation-2026-08-23.md` with
  before/after and ledger references
- Full suite green (`966 + 130` tests, `evals/ci_gate.py`)
- Committed and pushed to **both** remotes (`origin` and `gitea`)

---

## Task 3 -- the ~39 unaudited tools (follow-on, not now)

SOC, core, discovery, learning-loop, parser-factory, and CanonLoom lanes have
never been through the 6-axis rubric in `docs/tool-description-audit.md`.
**Order this work by Task 2's cluster ranking, not lane by lane** -- and note
that SOC already measures 95%, so its tools are unlikely to be where the
returns are. Same four rules as Task 1.

## Not in scope

Do not start these; they are decisions, not tasks:
- changing `BERSERK_MCP_ROLE`'s default away from `all` (breaking change)
- upgrading `find_tool` from TF-IDF to embedding-based retrieval (collides with
  the stdlib-only, zero-dependency constraint -- an architecture decision)
- local-hardware parity runs (blocked on whether hardware exists)

# Implementation specification: lane-scope the KQL-authoring escape hatches

Tracks GitHub issue [#4](https://github.com/ssimonsen0202/berserk_mcp/issues/4).
Written 2026-08-18 against commit `4e0172b`.

**Re-verify every line number before you edit.** Names are stable; line
numbers drift.

**Do not start this before issue #5 ships.** See "Dependency" below. Hiding
`search` from the small tier removes the fallback; #5 supplies the
replacement. Shipping this first leaves a lane with no way to answer a
question the fixed tools do not cover.

## Purpose

Role lanes filter by job function only. There is no notion of *tier*, so a
lane that should carry fixed-intent tools also carries every KQL-authoring
escape hatch and the whole CanonLoom artifact pipeline.

### Measured surface

Measured at `4e0172b` by importing the module per role and serialising
`_tool_list_result`. **These numbers supersede the estimates in
`docs/handoff-five-proposed-prs-2026-08-16.md`, which undercounted by about
ten tools per lane and understated the token cost by roughly 3x.**

| Lane | Tools | `tools/list` bytes |
|---|---:|---:|
| all | 69 | 68,318 |
| claude | 55 | 53,464 |
| sre | 43 | 40,796 |
| soc | 43 | 40,815 |
| ops | 35 | 32,116 |
| windows-forensics | 34 | 30,809 |

Bytes, not tokens — the token count depends on the tokenizer. As a rough
guide, divide by four: an ops lane costs about 8,000 tokens of tool
definitions before the model reads a single question.

Reproduce with:

```bash
python3 -c "
import os, json, importlib
for role in ['all','ops','sre','soc','claude','windows-forensics']:
    os.environ['BERSERK_MCP_ROLE']=role
    import berserk_mcp as bm; importlib.reload(bm)
    n=len([t for t in bm.TOOLS+bm.MGMT_TOOLS if bm.tool_visible(t)])
    b=len(json.dumps(bm._tool_list_result('legacy')))
    print(f'{role:20} tools={n:3} bytes={b:6}')"
```

### The escape hatches

**34 of 69 tools are untagged, so they ship in every lane**, including
`windows-forensics`:

```
list_containers  top_cpu  top_memory  errors_by_service  list_services
list_hosts  host_cpu  host_memory  container_hosts  logs_for_service
schema  list_metrics  bzrk_query_perf  discover_schema  self_check
search  trace_find_slow  trace_find_errors  trace_analyze
suggest_ingestion  canonloom_run_pipeline  canonloom_list_artifacts
canonloom_get_artifact  canonloom_freshness_report  canonloom_run_history
list_saved  run_saved  save_query  request_discovery  discovery_status
detect_new_sources  generate_parser  run_discovery_worker  review_generated
```

`validate_kql` is the only tool tagged with all four operational roles, which
is the same thing as untagged in effect.

Two consequences:

1. An on-call SRE lane carries the five `canonloom_*` artifact-lifecycle tools
   and the parser-generation pipeline as pure routing noise.
2. `search` ships everywhere with a description that invites use — "Use when
   the other tools do not fit". A 7B model that cannot find a fit will reach
   for it and author bad KQL, which is the problem the README opens by
   describing.

`evals/model-eval-plan.md` Part 3 already specifies the two-tier policy as
*measurement*: ≥95% tool selection, ≥90% argument correctness, ≤20%
escalation. This change is the enforcement half.

## Dependency

Issue #5 projects verified saved queries into `tools/list` as `saved__<name>`.
Until that ships, `search` is the only route to any question the fixed tools
do not cover, and hiding it from the small tier removes a capability without
supplying a replacement.

Confirm before starting: `saved__*` tools appear in `tools/list` on a store
with entries.

## Current architecture to preserve

Stdlib only. No new imports.

- `ACTIVE_ROLE` (`:140`) reads `BERSERK_MCP_ROLE`, defaults `"all"`.
- `_ROLE_PREFIX` (`:400`) holds the six known roles.
- F-008 (`:457`) exits at import on an unrecognized role. Read that comment
  before designing the tier check; it states the philosophy this change
  extends.
- `tool_visible(tool)` (`:473`) is the single visibility predicate.
- `_tool_list_result(mode)` (`:3065`) filters with it.
- `tools/call` re-applies the same predicate at `:3350` and returns
  `"unknown tool: " + name` for a role-hidden tool, deliberately not leaking
  that the tool exists.

## Functional requirements

### FR-1 Add a tier dimension orthogonal to lane

Tier answers "may this caller author KQL or drive the artifact pipeline?".
Lane answers "which job function?". They compose; neither replaces the other.

```python
TIER_SMALL = "small"
TIER_DEEP = "deep"

_DEEP_TIER_TOOLS = frozenset({
    "search", "validate_kql", "save_query",
    "generate_parser", "review_generated", "run_discovery_worker",
    "suggest_ingestion", "self_check",
    "canonloom_run_pipeline", "canonloom_list_artifacts",
    "canonloom_get_artifact", "canonloom_freshness_report",
    "canonloom_run_history",
})
```

Rationale for each inclusion:

- `search`, `validate_kql`, `save_query` — free-text KQL authoring.
- `generate_parser`, `run_discovery_worker`, `review_generated` — LLM-driven
  generation and its audit surface.
- `suggest_ingestion` — onboarding advice, not an operational answer.
- `self_check` — a wiring diagnostic; an operator or a deep-tier agent needs
  it, a small-tier router does not.
- `canonloom_*` — a separate service's artifact lifecycle. ADR-005 in
  `canonloom-blueprint` classes CanonLoom as a distinct platform from
  `berserk_mcp`; these five tools are a bridge, not core observability.

**Not** in the deep tier, and deliberately so:

- `list_saved` / `run_saved` and the `saved__*` projections from #5 — running
  a verified saved query is fixed-intent, which is exactly what the small
  tier is for.
- `discover_schema`, `schema` — reading a schema is how a small model avoids
  guessing field names. Removing it makes routing worse.
- `request_discovery`, `discovery_status`, `detect_new_sources` — queueing
  and status, no authoring.

### FR-2 Resolve the active tier

```python
ACTIVE_TIER = _choice_env("BERSERK_MCP_TIER", "", {"", "small", "deep"})
```

Resolution, in order:

1. `BERSERK_MCP_TIER` set to `small` or `deep` — use it. An explicit operator
   choice always wins.
2. Unset and `ACTIVE_ROLE == "all"` — tier is `deep`. `all` means "give me
   everything"; this change must not silently shrink it. Backwards compatible.
3. Unset and `ACTIVE_ROLE` is a single operational lane — tier is `small`.

Follow `_choice_env`'s existing failure behavior for an unrecognized value.
Check what it does before assuming; F-008 fails closed at import for a bad
role, and a bad tier should behave consistently with whatever `_choice_env`
already guarantees.

Document `BERSERK_MCP_TIER` in `.env.example` under "Role, primers, and
saved-query storage", or `EnvExampleDriftTest` fails the build.

### FR-3 Apply the tier in `tool_visible`

```python
def tool_visible(tool):
    roles = tool.get("roles")
    if roles and ACTIVE_ROLE != "all" and ACTIVE_ROLE not in roles:
        return False
    if ACTIVE_TIER_RESOLVED == TIER_SMALL and tool["name"] in _DEEP_TIER_TOOLS:
        return False
    return True
```

Keep the lane rule and the tier rule in one predicate. Two predicates will
drift, and `tools/call` enforcement at `:3350` calls this one function — that
is what makes F-008 hold. Do not add a second, tier-only check anywhere.

### FR-4 Log the applied tier loudly at startup

The main risk in this change is silently hiding a tool an operator relies on.
Mirror F-008's philosophy: fail loudly, or at minimum announce.

At import, after tier resolution, emit via `log()`:

```
tier=small (role=sre): 13 tools hidden — search, validate_kql, save_query, …
Set BERSERK_MCP_TIER=deep to restore them.
```

Name the count, the tools, and the exact escape hatch. An operator who did not
intend this must be able to see it and undo it from the log line alone.

### FR-5 `self_check` reports the tier

`--doctor` and `self_check` exist to answer "is this wired the way I think?".
A hidden tool is exactly that class of surprise.

Add a check to `_run_doctor_checks()` reporting the resolved tier, the count
of hidden tools, and the env var that changes it. Status `pass`,
`required=False` — a small tier is a valid configuration, not a fault.

## Non-goals

- No new tools.
- No change to what any tool does. This is visibility only.
- No change to the `all` lane's surface.
- No per-tool tier overrides beyond the single set in FR-1. A configurable
  per-tool tier map is a second policy surface to keep in sync; if it turns
  out to be needed, that is a separate spec.

## Test plan

Extend `tests/test_roles.py` — it already covers lane filtering and is the
natural home — or add `ToolTierTest` to `tests/test_berserk_mcp.py` if the
role tests do not fit the pattern.

Write each test first. Read `docs/claude-code-review-feedback-loop.md` before
starting; fault 2 ("fixing one call site when the concern spans the dispatch
table") applies directly, and so does fault 6 on test isolation, since tier
resolution reads module-level state resolved at import.

Required cases:

1. `role=all`, tier unset: surface is byte-identical to the pre-change
   `tools/list`. This is the backwards-compatibility guarantee.
2. `role=sre`, tier unset: every tool in `_DEEP_TIER_TOOLS` is absent.
3. `role=sre`, `BERSERK_MCP_TIER=deep`: the deep tools return.
4. `role=all`, `BERSERK_MCP_TIER=small`: an explicit small tier applies even
   on `all` — FR-2 rule 1 beats rule 2.
5. `tools/call` on a tier-hidden tool returns `"unknown tool: <name>"` with
   `is_error=True`, identical to the role-hidden case. The F-008 rule.
6. `list_saved`, `run_saved`, `discover_schema`, and a `saved__*` projection
   stay visible in the small tier.
7. The startup log line names the hidden count and `BERSERK_MCP_TIER=deep`.
8. `self_check` reports the resolved tier.

Tier resolution happens at import, so tests must `importlib.reload(bm)` under
a patched environment and restore afterwards. Follow the existing pattern in
`test_roles.py`.

### Eval and CI work

`evals/router_cases.jsonl` currently carries 41 cases and no lane or tier
dimension. Add:

1. A `lane` (and where relevant `tier`) label per case.
2. Confusable-pair cases. The `top_cpu` vs `host_cpu` and `top_memory` vs
   `host_memory` disambiguation is already written into the tool descriptions
   as prose; promote it to cases so it is measured, not just asserted.
3. Cases where the correct answer in a small tier is a `saved__*` tool, and
   the same prompt in a deep tier may legitimately use `search`.

Wire the eval harness into `.github/workflows/ci.yml` as a plumbing gate:

```yaml
- name: Router eval (mock backend)
  run: python evals/run_eval.py --backend mock evals/router_cases.jsonl
```

CI today runs unittest plus the protocol smoke test and never invokes the
eval harness. Confirm `--backend mock` runs without network or an LLM before
adding it; if it does not, add the flag rather than adding a gate that needs
credentials.

Emit per-lane tool count and `tools/list` byte size as a CI artifact, using
the reproduction command above. That artifact is also the guard rail for
issue #5's projection cap — a reviewer should be able to see surface growth
in the PR without running anything.

## Acceptance

Run, and paste real output into the PR:

```bash
python3 tests/test_berserk_mcp.py
python3 -m unittest discover -s tests
python3 evals/mcp_protocol_smoke.py --include-http
python3 evals/run_eval.py --backend mock evals/router_cases.jsonl
```

Then, before writing that CI passes:

```bash
gh pr checks <n> --repo ssimonsen0202/berserk_mcp
```

Report the before/after table for all six lanes. The small tier should remove
roughly 12,000-13,000 bytes from an operational lane. If it removes
substantially less, the tier set is too small to be worth the added
configuration surface — say so in the PR rather than shipping it.

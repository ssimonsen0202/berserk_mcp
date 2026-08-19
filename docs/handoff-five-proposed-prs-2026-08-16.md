# berserk_mcp — five proposed pull requests

> **Corrections, 2026-08-18.** Two premises in this brief are wrong. Read
> these before acting on anything below.
>
> 1. **There is no `berserk-blueprint` repo, by decision.** This brief's
>    ground rule names a three-repo separation of
>    `berserk-blueprint` / `berserk_mcp` / `berserk-knowledge`. Those are the
>    **CanonLoom** repos, renamed by
>    [ADR-005](../../canonloom-blueprint/docs/06-adrs/ADR-005-canonloom-platform-identity.md)
>    (Accepted 2026-08-02) precisely because the old name collided with the
>    bzrk.dev product and with "the unrelated `berserk_mcp` query server" —
>    ADR-005's own words. Design authority is `canonloom-blueprint`; governed
>    corpus is `canonloom-knowledge`; implementation is `canonloom`.
>    `berserk_mcp` is not governed by them. Its specs live in its own `docs/`,
>    following `docs/validate-kql-implementation-spec.md`. Creating a
>    `berserk-blueprint` would revive the collision ADR-005 exists to end.
>
> 2. **The surface measurements are stale and undercounted.** Measured at
>    `4e0172b`: all 69 tools / 68,318 bytes, claude 55 / 53,464, sre 43 /
>    40,796, soc 43 / 40,815, ops 35 / 32,116, windows-forensics 34 / 30,809.
>    That is roughly ten more tools per lane than stated below, and about 3x
>    the token cost. 34 of 69 tools are untagged and ship in every lane. See
>    `docs/tool-tiers-implementation-spec.md` for the reproduction command.
>
> Specs written from this brief, with both corrections applied:
> `docs/saved-queries-as-tools-implementation-spec.md` (PR 4 / issue #5),
> `docs/tool-tiers-implementation-spec.md` (PR 3 / issue #4),
> `docs/result-envelope-implementation-spec.md` (PR 1 / issue #2).

Handoff brief for a future Claude Code session (Sonnet 4.6 or similar).
Derived from a read of `ssimonsen0202/berserk_mcp` at commit `da85ac6`
(v1.25.1), re-verified against local commit `6acaaa9` on
`fix/search-wide-projection-json-mode` (2026-08-16) — see "Line-number
corrections" below. **Re-verify every line number again before editing**;
the file is ~3,800 lines and moves.

Filed for later work, not started. No branches created, nothing pushed.

---

## Line-number corrections (verified 2026-08-16 against commit 6acaaa9)

The original brief was written against `da85ac6`. One commit has landed
since (`6acaaa9`, the WIDE_PROJECTION `search` fix, +10 lines around
line ~2280), so everything after that point in the file has shifted by
roughly +10 lines. Spot-checked anchors:

| Anchor | Brief said | Actually at (6acaaa9) |
|---|---|---|
| `SIMPLE = {` dict start | :1553 | 1553 (confirmed exact) |
| F-008 fail-fast comment | :466-471 | 457 (first occurrence); second occurrence also exists at 3178 (tools/call enforcement) — brief only cited the first |
| `_BASE_INSTRUCTIONS = (` | :386-398 | 386 (confirmed exact) |
| `AUTH_FAILURE_MESSAGE` | :900 | 900 (confirmed exact) |
| `def main():` | :3520 | 3530 |
| `capabilities.tools.listChanged` (x2) | :2717, :3116 | 2727, 3126 |
| Total file length | not stated | 3,804 lines |

Other claims spot-checked and confirmed accurate:
- `.env.example` documents exactly **11** vars (all HTTP transport + the
  2026-07-28 gate), all commented out by default. Matches brief exactly.
- `evals/router_cases.jsonl` is 41 lines; `expect_since_any` appears
  **exactly once** (on `errors_24h`), matching the brief's claim that only
  one case uses it.
- README line 190 confirms the exact "named, reusable tools" comparison-row
  wording PR 4 cites.
- Distinct `os.environ.get(...)` reads across `berserk_mcp.py`,
  `agent_analytics.py`, `ai_finops.py`, `ingestion_advisor.py`,
  `schema_registry.py`, `kql_validation.py`, `parser_factory.py`: 41 (brief
  says "42 distinct environment variables" — close; re-count against
  whichever files the future session considers in scope before treating 42
  as exact).

---

## 0. Ground rules for the agent

- **Repo discipline.** Three-repository separation applies: `berserk-blueprint`
  is design authority, `berserk_mcp` is implementation, `berserk-knowledge` is
  validated capabilities. PRs 1, 3 and 4 change observable contracts and need a
  blueprint spec before any implementation diff. PRs 2 and 5 are
  implementation-local.
- **Review before run.** Propose diffs; do not push, do not open PRs, do not
  run network/install/admin commands without explicit approval. Stay inside the
  project directory.
- **Zero dependencies is a hard constraint.** stdlib only. Nothing in these
  five PRs requires otherwise. Any proposal that adds a third-party import is
  wrong and should be re-scoped.
- **Minimal diffs.** UTC timestamps, stable output objects, existing formatting
  conventions. Report exact changed-file summaries.
- **One PR per branch.** Do not combine. Each has its own acceptance tests.
- **Remotes note (added 2026-08-16):** this working copy's `gitea` remote is
  misconfigured — it points at `nuc-assistant.git`, not a berserk-mcp repo.
  `github` (`https://github.com/ssimonsen0202/berserk_mcp.git`) is the correct,
  working remote; use it unless the user has since fixed the Gitea remote.
  Auto-mode blocks `git push` without explicit user action — expect to hand
  the push command back to the user rather than running it directly.

### The bar every PR is judged against

The README makes three commitments. Every change below either serves one or is
out of scope.

1. **Fixed queries so weak models route reliably** — the model picks an intent
   and a window; it never authors KQL.
2. **Conclusions, not row dumps** — "a verdict, a baseline deviation, a cost
   trend."
3. **Sovereign two-tier local** — a small open-weight model handles ≥80% of
   interactions, escalating to a larger local model only for `@deep` work.
   Targets in `evals/model-eval-plan.md` Part 3: ≥95% tool selection, ≥90%
   argument correctness, ≤20% escalation.

---

## PR 1 — Result envelope for the fixed-query path

**Branch:** `feat/simple-result-envelope`
**Needs blueprint spec:** yes (changes output shape of 24 tools)

### Gap

24 of the highest-traffic tools dispatch through `SIMPLE` (`:1553-1577`) and
return `bzrk` stdout verbatim (`:2191-2194`, now shifted ~+10):

```python
if name in SIMPLE:
    kql, default_since = SIMPLE[name]
    since = arguments.get("since") or default_since
    return bzrk_search(kql, since)
```

Two consequences:

- **The window is invisible.** Defaults differ per tool (15m `top_cpu`, 1h
  `errors_by_service`, 6h `soc_repeated_errors`) and are never echoed. A model
  that omits `since` gets rows with no idea what they span, then writes "there
  were 12 errors" — a wrong sentence built on a correct query.
- **Empty results are a bare sentinel.** `bzrk_search` returns the literal
  `(no rows)` (:1010, now shifted). For a small model that string is ambiguous
  between *healthy*, *wrong window*, *wrong tool*, and *source stopped
  ingesting*. In practice it produces confident false negatives or blind
  retries.

`detect_anomalies` and `forecast_capacity` (originally :2108-2145) already do
this correctly — they interpret, name the window, and refuse rather than
invent. The pattern exists; it just isn't applied to the tools called most.

Related: the `stdout_overflow` error (originally :999-1004) tells the model to
"project fewer columns, or add a smaller take/top/tail bound." On a `SIMPLE`
tool the query is fixed — the model's only lever is `since`. Fix that message
on the fixed-query path while you're in here.

### Change

- Add `_envelope(tool, since, out)` applied on the `SIMPLE` path only.
- Emit resolved window + row count as a short header, then the **raw rows
  verbatim underneath** so nothing downstream that greps output breaks.
- Static per-tool next-step table for the empty case. No LLM, no new queries.
  Example: `errors_by_service` empty → "No ERROR rows in <window>. Widen with
  `since='24h ago'`, or confirm the source is reporting with `list_services`."
- Gate behind `BERSERK_MCP_ENVELOPE=0` for one release cycle.
- Rewrite the overflow message for fixed-query tools to name `since` as the
  only available lever.

### Acceptance

- Unit tests with stubbed `bzrk` (pattern already established in `tests/`) —
  one per envelope case: rows present, `(no rows)`, overflow, auth failure.
- Router/answer cases where the pass condition is "model does not assert a
  false negative" given a `(no rows)` response.
- Existing `tests/test_berserk_mcp.py` and `evals/mcp_protocol_smoke.py` must
  pass unchanged with the env gate off.

---

## PR 2 — Make `since` machine-constrained and forgiving

**Branch:** `feat/since-schema-and-normalizer`
**Needs blueprint spec:** no — implementation-local, additive

**Ship this one first.** Lowest risk, highest immediate routing return.

### Gap

`since` appears on **51 of 59 tools**. Its schema (originally :1472-1473) is:

```python
{"since": {"type": "string",
           "description": "Time window e.g. '15m ago', '1h ago', '2d ago'."}}
```

No `pattern`, no `enum`, no `examples`, no `default`. The only `pattern` in the
entire tool surface is on `claude_record_recommendation_decision`. Validation is
`_SINCE_RE` (originally :1042), accepting `now` or `<int><unit>[ ago]`; a miss
costs a full round trip (originally :1079-1083).

This matters twice:

- **Grammar-constrained decoding is the main lever for small-model
  reliability** — structured outputs, GBNF, JSON-schema-constrained sampling.
  All of them need something in `inputSchema` to constrain against. Today
  there's nothing, so `since` is the last field on the fixed-query path where
  the model is free to guess syntax. That is the exact failure mode the project
  exists to eliminate, surviving on 86% of tools.
- **The forms small models actually emit all fail**: `last 24 hours`,
  `yesterday`, `past week`, `2026-08-15`. (`24h` happens to pass; `last 24
  hours` does not.)

### Change

1. Add `pattern`, `examples`, and an explicit per-tool `default` to the `since`
   schema. The `default` also makes the currently invisible per-tool window
   visible to the model — this partially overlaps PR 1 and that is fine.
2. Add `_normalize_since()` at the door, mapping common natural forms onto the
   canonical grammar **before** validation.
3. Keep `_SINCE_RE` as the post-normalization gate. **The normalizer must emit
   only canonical forms** — nothing new reaches `bzrk` and the validation
   posture is unchanged. This is the security-relevant invariant; assert it.

### Acceptance

- Table-driven test over ~30 model-realistic strings: accepted-and-normalized,
  accepted-unchanged, and correctly-rejected buckets.
- Property test or explicit assertion: for every input the normalizer accepts,
  its output satisfies `_SINCE_RE`.
- Add `expect_since_any` assertions to more cases in `evals/router_cases.jsonl`
  — the field exists but only `errors_24h` uses it (confirmed: exactly 1 of 41
  lines as of 2026-08-16).
- Report retry-rate delta from `evals/run_eval.py` before/after.

---

## PR 3 — Lane-scope the KQL-authoring escape hatches

**Branch:** `feat/tool-tiers`
**Needs blueprint spec:** yes (changes tool visibility)
**Depends on PR 4.** See sequencing note at the end.

### Gap

Role lanes filter by job function only, with no notion of *tier*. Measured
surface at `da85ac6`:

| Lane   | Tools | ~tokens in `tools/list` |
|--------|-------|--------------------------|
| ops    | 25    | ~2,600                   |
| sre    | 33    | ~3,450                   |
| soc    | 33    | ~3,470                   |
| claude | 45    | ~4,740                   |

`search` — free-text KQL — is **untagged, so it ships in every lane**
(originally :1629, confirmed the tool def is still there, unaffected by the
recent JSON-mode fix which only touched the dispatch handler further down),
with a description that actively invites use ("Use when the other tools do
not fit"). So do `validate_kql` (tagged with all four roles, i.e. effectively
ALL), `suggest_ingestion`, and all five `canonloom_*` artifact-lifecycle
tools. An on-call SRE lane carries the CanonLoom pipeline as pure routing
noise, and a 7B model that can't find a fit will reach for `search` and
author bad KQL — reproducing the exact problem the README opens by
describing.

### Change

- Introduce a `tier` dimension orthogonal to lane.
  - **small tier:** fixed-intent tools only.
  - **author/deep tier:** `search`, `validate_kql`, `save_query`,
    `generate_parser`, `review_generated`, `canonloom_*`, `suggest_ingestion`,
    plus `self_check` from PR 5.
- Default small-tier-on when `BERSERK_MCP_ROLE` is a single operational lane.
  Leave `all` untouched for backwards compatibility.
- **Mirror the existing F-008 fail-fast pattern** (originally :466-471, now at
  line 457 for the first occurrence, with the tools/call enforcement mirror at
  line 3178) and log the applied tier at startup. Silently hiding a tool an
  operator relies on is the main risk here; make it loud.

This is the enforcement half of the two-tier policy that
`evals/model-eval-plan.md` Part 3 currently specifies only as measurement.

### Acceptance

- `evals/router_cases.jsonl` covers 31 cases across 21 of 59 tools (41 total
  lines in the file as of 2026-08-16; re-verify the 31/21 figures against
  current content) and carries no lane dimension. Add lane labels and
  confusable-pair cases — the `top_cpu` vs `host_cpu` and `top_memory` vs
  `host_memory` disambiguation is already written into the tool descriptions
  as prose; promote it to cases.
- Wire `python evals/run_eval.py --backend mock evals/router_cases.jsonl` into
  `.github/workflows/ci.yml` as a plumbing gate (file confirmed present at
  `.github/workflows/ci.yml`). CI today runs unittest + protocol smoke and
  never invokes the eval harness.
- Emit per-lane tool count and `tools/list` byte size as a CI artifact so
  surface growth is visible in the PR diff. This artifact is also the guard
  rail for PR 4.

---

## PR 4 — Promote saved and generated query packs into `tools/list`

**Branch:** `feat/project-saved-queries-as-tools`
**Needs blueprint spec:** yes (changes the tool surface and MCP capabilities)

### Gap

The README comparison table claims custom-query persistence yields "named,
reusable tools" (confirmed verbatim at README.md:190: *"**Custom-query
persistence** as named, reusable tools | UI has a Query Library. Berserk
documents no API or CLI verb to create, list, or share a saved query
programmatically | ✅ `save_query` (verify-before-persist) → `run_saved`,
agent-readable"*). It doesn't fully deliver on this. Saved queries are rows in
a JSON store reachable only through a two-step prose indirection (originally
:1859-1893):

```
list_saved  →  "- disk_pressure_by_host: which hosts are near disk saturation"
            →  run_saved(name="disk_pressure_by_host")
```

They never appear in `tools/list`. `_BASE_INSTRUCTIONS` (confirmed at
:386-398) tells the model how to *create* a saved query but never mentions
that any exist, and never names `list_saved` — so on a cold session the model
must speculatively probe a store it has no reason to believe is populated.

This is where the most valuable machinery in the repo deposits its output.
`generate_parser` and `run_discovery_worker` write into the same store with
`origin: "generated"` (originally :1364). Auto-authored, execute-verified
query packs for newly onboarded sources are the *least* discoverable part of
the surface. That is backwards for a fixed-query design: verified intents
should become more first-class over time, not sit behind a lookup costing two
round trips and a prose parse — on exactly the model tier that handles both
worst.

### Change

- Project visible learned items into `tools/list` as `saved__<name>`:
  description from the stored description, `since` default from the stored
  value, `roles` honoured via the existing `item_visible`.
- Dispatch maps straight back onto the current `run_saved` path. Validation,
  schema-drift check and budget logic do not move.
- Flip `capabilities.tools.listChanged` — hardcoded `false` in both places
  (confirmed at lines 2727 and 3126 as of commit 6acaaa9, was :2717/:3116 in
  the brief's original commit) — and emit `notifications/tools/list_changed`
  after a successful `save_query` or worker write, so a query saved mid-session
  becomes callable without a reconnect.
- **Cap the projection** (N most recent, role-scoped) and keep generated
  descriptions inside their existing `<generated-description>` fencing so the
  v1.23 sanitization posture holds.

### Acceptance

- `evals/mcp_protocol_smoke.py` asserts a saved query appears in `tools/list`
  and is directly callable.
- Assert `notifications/tools/list_changed` fires after `save_query` and does
  not fire on a rejected save.
- Router cases where the correct answer is a projected saved tool rather than
  `search`.
- Per-lane tool count stays under the cap enforced by PR 3's CI artifact.

---

## PR 5 — `--doctor` preflight and a `self_check` tool

**Branch:** `feat/doctor-preflight`
**Needs blueprint spec:** no — implementation-local, additive

### Gap

The code reads roughly **41-42 distinct environment variables** across its
modules (41 confirmed 2026-08-16 across `berserk_mcp.py`,
`agent_analytics.py`, `ai_finops.py`, `ingestion_advisor.py`,
`schema_registry.py`, `kql_validation.py`, `parser_factory.py` — re-count if
scope differs). `.env.example` documents exactly **11** (confirmed exact
count 2026-08-16) — the HTTP transport block plus the 2026 protocol gate.
`BZRK_BIN`, `BZRK_PROFILE`, `BZRK_TIMEOUT`, `BERSERK_TABLE`,
`BERSERK_MCP_ROLE`, `BERSERK_MCP_REDACT*`, `BERSERK_MCP_LEARNED_PATH`,
`BERSERK_MCP_PSEUDONYM_KEY`, the LLM-ladder vars and the CanonLoom pair are
documented only inside a 1,669-line / 109 KB README.

`main()` (confirmed at line 3530, was :3520 in the brief's original commit)
carries 16 CLI flags and no validation entry point. The server does exactly
one fail-fast check at import: an unknown `BERSERK_MCP_ROLE` (confirmed at
line 457, the F-008 comment; a second F-008 mirror exists at line 3178 for
tools/call enforcement, not mentioned in the original brief). Everything else
fails late and opaquely — `bzrk` missing from PATH, stale auth, wrong profile,
wrong `BERSERK_TABLE`, unreadable primers dir, an empty database.
`AUTH_FAILURE_MESSAGE` (confirmed at line 900) is a good string, but it
arrives at question time, addressed to an agent rather than an operator. And
the agent cannot distinguish "wired wrong" from "genuinely nothing to
report."

The README's strongest positioning is air-gapped / sovereign / fleet deployment
on hardware you own. Those are precisely the environments where you cannot
iterate interactively. The F-008 comment already states the right philosophy;
this extends it from one variable to the whole config surface.

### Change

- `berserk-mcp --doctor`: ordered checks, pass/fail/skip table, one exact
  remediation line per failure.
  1. `bzrk` resolvable via `BZRK_BIN` or PATH
  2. `bzrk` version
  3. auth under the configured `BZRK_PROFILE`
  4. `BERSERK_TABLE` reachable
  5. row count in the last 1h
  6. primers dir resolvable for the active `BERSERK_MCP_ROLE`
  7. learned-store path writable
  8. HTTP config internally coherent when `BERSERK_MCP_HTTP_ENABLE=1`
  9. LLM ladder / CanonLoom reachability — **skipped when unconfigured, not
     failed**
- Exit codes `0` pass / `1` degraded / `2` broken, so it works as a fleet
  readiness probe and a container healthcheck.
- Same checks behind a `self_check` tool, author/deep tier per PR 3, so an
  agent hitting repeated errors can diagnose wiring instead of reporting a
  false all-clear.
- Extend `.env.example` to all ~41-42 vars, grouped, each with the default the
  code actually applies. Doubles as an audit artifact for the "read it in an
  afternoon" claim.

### Acceptance

- Stubbed-`bzrk` unit tests per failure mode, independently triggerable.
- CI runs `--doctor` against the stub and asserts each exit code.
- Drift test: grep the codebase for env reads, fail the build if
  `.env.example` doesn't cover them.
- `--doctor` respects existing budget/timeout guards and routes anything it
  echoes through `secret_scan.py` redaction. Assert no secret value is ever
  printed.

---

## Sequencing

```
PR 2 (since schema)      ──┐
PR 5 (doctor)            ──┤  independent, implementation-local, ship first
                           │
PR 1 (result envelope)   ──┤  blueprint spec first
                           │
PR 4 (saved → tools)     ──┴──> PR 3 (tiers)
```

**PR 4 is a precondition for PR 3 being defensible.** Hiding the KQL escape
hatch from the small tier is only reasonable once verified saved intents are
first-class in `tools/list` — otherwise you remove the fallback without
supplying the replacement.

## Explicitly out of scope

Adding more tools. At 59 tools and ~3.5k tokens per lane, the routing surface
is already the binding constraint on the small-model thesis. Every PR above is
subtractive or clarifying. Any proposal that grows the fixed tool count should
be rejected unless it comes with a corresponding removal.

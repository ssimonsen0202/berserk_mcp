# Implementation plan — validating and sequencing the 2026-08-16 strategy brief

Written 2026-08-20 against `main` at `2523b33`, in response to the task brief in
`START-HERE.md` / `berserk-mcp-strategy-and-backlog.md` / `berserk-dev-brief.md`
(all three supplied externally on 2026-08-16, external to this repo).

**Where this lives, and why.** `START-HERE.md` calls for a separate
`berserk-blueprint` repo as design authority. That repo doesn't exist, and
shouldn't: [ADR-005 in `canonloom-blueprint`](../../canonloom-blueprint/docs/06-adrs/ADR-005-canonloom-platform-identity.md)
(Accepted 2026-08-02 — *before* this brief was written) retired the name
"Berserk" for a design-authority repo specifically because it collided with
this project. This plan lives in `berserk_mcp/docs/`, alongside the specs
already written this way for issues #2, #4, and #5.

## Phase 1 — Reconciliation

The brief was written against `da85ac6`, partially reconciled to `66be753`.
`main` has moved substantially further:

```
66be753 (brief's reconciliation baseline)
  → e257fda  fix(search): body-bearing tools always use JSON        [#1]
  → 4e0172b  feat(doctor): --doctor preflight, self_check tool      [#8, BM-3]
  → 6dfadf5  feat(saved-queries): project saved queries into tools/list  [#9, BM-4]
  → 2523b33  docs: add market/reliability brief for the dev team    (this brief itself, filed)
```

Plus **PR #10** (`feat/result-envelope`, BM-1), currently open with green CI,
not yet merged as of this writing.

Four of the backlog's Epic A items are further along than the brief knew:

| BM # | Item | Actual state |
|---|---|---|
| BM-1 | Result envelope | **In flight** — PR #10, CI green, spec at `docs/result-envelope-implementation-spec.md` |
| BM-2 | `since` normalizer | **Shipped** — PR #7, merged *before* this brief was written (the brief says as much) |
| BM-3 | `--doctor` preflight | **Shipped** — PR #8 |
| BM-4 | Saved queries in `tools/list` | **Shipped** — PR #9. Shipped *before* discovery (BM-6), the reverse of the brief's own risk note ("consider sequencing BM-4 after BM-6") |

**Measured baseline, re-run at `main`** (the brief's §1.2 table, recomputed —
report the number, don't trust the old one):

| Metric | Brief's value (`da85ac6`) | Actual (`main`) |
|---|---|---|
| Tools in `TOOLS`+`MGMT_TOOLS` | 59 | **69** |
| Env vars read | 42 | **61** (found via multi-line-aware scan; see `docs/handoff-five-proposed-prs-2026-08-16.md`'s correction note) |
| Env vars documented | 11 | **all of them** — `.env.example` now has a drift-guard test |
| Router eval cases | 31, 21/59 tools | **41, 24/69 tools** |
| `expect_since_any` usage | "only `errors_24h`" | still **1 of 41** — unaddressed |
| CI eval invocation | none | **still none** — confirmed, `run_eval.py` doesn't appear in `.github/workflows/` |
| README size | 1,669 lines | **1,669 lines, unchanged** |

**Also: the source documents disagree with each other.** `START-HERE.md` says
"issues BM-1 … BM-28"; `berserk-mcp-strategy-and-backlog.md` only defines
BM-1 through BM-19. Treat BM-20 through BM-28 as not existing — there is
nothing to verify.

## Phase 2 — Verdict table

Per `START-HERE.md`'s instruction: `CONFIRMED` / `STALE` / `WRONG` / `UNCLEAR`,
one line each, adversarial.

| BM # | Item | Verdict | Basis |
|---|---|---|---|
| BM-1 | Result envelope | **STALE** | In flight as PR #10 (issue #2). Same design — window echo, `(no rows)` disambiguation, overflow message fix — verified by direct code review, not just the PR description. |
| BM-2 | `since` schema + normalizer | **STALE** | PR #7, merged before this brief. |
| BM-3 | `--doctor` / `self_check` | **STALE** | PR #8, merged. All 9 checks from the brief's list present in `_run_doctor_checks()`. |
| BM-4 | Project saved queries into `tools/list` | **STALE** | PR #9, merged. `saved__<name>` projection, cap, `listChanged` transport-aware — matches the brief's change list closely enough that the brief and the shipped spec were clearly convergent, independently. |
| BM-5 | Wrong-answer containment: name it, test it | **CONFIRMED** | Grepped README + docs for a consolidated "wrong-answer containment" section — doesn't exist. "silent-failure" appears only in an incidental v1.14.1 release-note context, not as a named, documented set of invariants. The underlying mechanisms (`_SINCE_RE`, `_validate_user_kql`, schema-drift warnings) do exist; they're genuinely scattered and unnamed, as claimed. |
| BM-6 | `find_tool` JIT discovery | **CONFIRMED** | Grepped for `find_tool` — zero hits. Does not exist in any form. |
| BM-7 | Rewrite instructions for discovery loop | **CONFIRMED, but only as a consequence of BM-6** | `_BASE_INSTRUCTIONS` still names `top_cpu`, `errors_by_service`, `logs_for_service`, `host_cpu` verbatim (read the live string). Correctly un-actionable until BM-6 exists — there's no discovery loop to write instructions *for* yet. |
| BM-8 | Tool tiers | **CONFIRMED — spec already written, not implemented** | `docs/tool-tiers-implementation-spec.md` exists (issue #4, written 2026-08-18, independently of this brief). Confirmed no `TIER_SMALL`/`_DEEP_TIER_TOOLS`/`BERSERK_MCP_TIER` in code — spec only, unimplemented. The brief's own gap description (34 of 69 — not 59 — tools untagged, `search` ships everywhere) matches my spec's measurement almost exactly, arrived at independently. |
| BM-9 | Router eval expansion + CI gate | **CONFIRMED** | 41 cases (not 31), 24/69 tools (not 21/59) — grown since the brief, but proportionally similar coverage (~35%). No lane field on any case. No `run_eval` invocation anywhere in `.github/workflows/`. |
| BM-10 | Principal refactor | **CONFIRMED** | `ACTIVE_ROLE` is still `os.environ.get("BERSERK_MCP_ROLE", "all")` — a module-level global, exactly as described. No `Principal` class anywhere. |
| BM-11 | Hash-chained audit ledger | **CONFIRMED** | No `audit.py`, no hash-chain fields anywhere in `berserk_mcp.py`. |
| BM-12 | Scoped principals (HTTP) | **CONFIRMED** | No `mint-token`, no `token_hash`. `BERSERK_MCP_HTTP_AUTH_TOKEN` is still a single shared-secret check. |
| BM-13 | Audit export/verify CLI | **CONFIRMED (trivially — depends entirely on BM-11, which doesn't exist)** | Nothing to verify beyond BM-11's absence. |
| BM-14 | Ledger as eval corpus | **CONFIRMED (same — depends on BM-11 and BM-6, neither exists)** | Same. |
| BM-15 | Backend abstraction layer | **WITHDRAWN, not verified** | `START-HERE.md` §1 explicitly withdraws Epic D: "No multi-backend adapter." Per its own scope constraint, not evaluated further. |
| BM-16 | Second backend adapter | **WITHDRAWN** | Same. |
| BM-17 | Cross-backend provenance | **WITHDRAWN** | Same. |
| BM-18 | README restructure | **CONFIRMED** | `wc -l README.md` → exactly 1,669 lines, matching the brief's figure precisely. Unchanged, still a single file. |
| BM-19 | Routing reliability analyzer | **CONFIRMED** | No matching file anywhere in the repo. |

### Adversarial challenges, per `START-HERE.md`'s explicit ask

**The dependency claim is wrong for two of the three items it names.**
`START-HERE.md` and the backlog both assert "BM-10 (principal refactor)
lands before BM-6, BM-8 and BM-11." I have direct, concrete evidence against
this for BM-8, and a structural argument against it for BM-6 and BM-11:

- **BM-8 (tool tiers):** I *already wrote* `docs/tool-tiers-implementation-spec.md`
  for this exact item (as issue #4), independently, before reading this
  backlog. Its design resolves tier from `ACTIVE_ROLE`/`BERSERK_MCP_TIER` at
  the same process-global level `tool_visible()` already operates at — no
  `Principal` object anywhere in the design. It doesn't need BM-10. This
  isn't a hypothetical: it's a spec that exists and would work today.
- **BM-6 (discovery):** the backlog's justification is "index must be
  principal-filtered." But the *existing* static tool list is already
  filtered by a process-global `ACTIVE_ROLE`, not a per-request principal —
  today's architecture is single-principal-per-process on both transports
  (stdio always; HTTP too, until BM-12 ships scoped tokens). A discovery
  index filtered the same way `tool_visible()` already filters is
  consistent with the current architecture and needs no refactor first.
- **BM-11 (ledger):** "needs a principal to attribute to" conflates *richer*
  attribution with *any* attribution. A ledger could record
  `principal_id: <role>` against the existing process-global identity today
  — shallower than post-BM-10 per-request attribution, but not blocked.

**Revised claim:** BM-10 blocks the *richest* version of BM-6/BM-8/BM-11
(per-request, per-token attribution), not their existence. All three can
ship against the current process-global model and be upgraded once BM-10
lands. This changes the milestone ordering in Phase 4 below.

**Sizing labels, challenged per the ask:**

- **BM-6 "L" (1-2 weeks)** — plausible for the index/retrieval-quality work,
  but the brief's own risk note says "risk is concentrated in retrieval
  quality, not code," which argues the *code* is closer to M and the L
  estimate is really "M code + unbounded-until-measured tuning time." Keep
  the label but don't budget it as fixed-scope engineering time.
- **BM-9 "M"** — defensible, but understated relative to what full coverage
  actually requires: 45 more tools need ≥3 phrasings each (the brief's own
  acceptance criterion) — that's ~135 new cases by hand, which is closer to
  a full sprint than "2-4 days" once you include the confusable-pair and
  lane-labeling work layered on top in the same item.
- **BM-1/BM-2/BM-3/BM-4 sizes** — moot; already shipped, actual size is
  known from the merged PRs rather than estimated. Worth noting for
  calibration: BM-1 (labeled M) took 3 Codex review rounds and ~10 new
  tests before merge-ready; BM-4 (labeled M) took 2 review rounds including
  a real P1 security finding (secret redaction bypass). "M" undersold both
  in practice. Treat the unshipped M/L labels in Epic B/C with the same
  skepticism.

**BM-28 does not exist.** Flagging per `START-HERE.md`'s own instruction to
report source-document inconsistencies rather than silently resolve them.

## Phase 3 — Sizing for CONFIRMED items

Only items with real, unimplemented gaps. (Skipping BM-1–4: shipped. Skipping
BM-15–17: withdrawn.)

| BM # | Files touched | Rough LOC | What it breaks | Must land first |
|---|---|---|---|---|
| BM-5 | `README.md`, new eval cases, no `berserk_mcp.py` change if controls already hold | ~50-150 (mostly docs + tests) | Nothing — additive | Nothing |
| BM-6 | `berserk_mcp.py` (new `find_tool`, keyword index), `.env.example`, tests, `evals/` | 400-600 + retrieval tuning | `tools/list` shape (adds a tool), every lane's advertised surface, client caching assumptions (`ttlMs`/`cacheScope`) | Nothing structural (see dependency challenge above) — but BM-7 must ship same-release or instructions go stale |
| BM-7 | `berserk_mcp.py` (`_BASE_INSTRUCTIONS`, `_ROLE_PREFIX`, `primers/*.md`) | ~50-100 | Prompt content only | BM-6 |
| BM-8 | `berserk_mcp.py` per `docs/tool-tiers-implementation-spec.md` (already written) | ~150-250 per that spec | `tools/list` surface per lane, `tools/call` enforcement | Issue #5 (saved-queries) — **already shipped**, so BM-8 is actually unblocked right now |
| BM-9 | `evals/router_cases.jsonl`, `evals/run_eval.py`, `.github/workflows/ci.yml` | ~200 (harness) + ~135 new cases | CI gate — first hard eval gate in the repo | Nothing, but most valuable after BM-8 (needs lane labels) |
| BM-10 | `berserk_mcp.py` broadly — every read of `ACTIVE_ROLE`, `tool_visible`, `item_visible`, `build_instructions`, `normalize_roles` | 300-500, touches ~15-20 call sites | The visibility/security path — highest blast radius in the backlog | Nothing, but should land before BM-11/BM-12's *richer* versions |
| BM-11 | New `audit.py`, `_store.py` extension (append-only path), `berserk_mcp.py` (emit points) | 300-500 | Adds a new file-format surface with its own retention/locking model | Nothing structurally, per the dependency challenge — but a shallow `principal_id` today gets richer once BM-10 lands, so sequencing BM-10 first avoids a rework |
| BM-12 | `berserk_mcp.py` (HTTP auth path), new principal-store logic | 200-400 | Auth path — security-sensitive, needs the most review | BM-10 (this one *is* a real dependency — scoped tokens need the Principal object BM-10 introduces) |
| BM-13 | New CLI flags, `audit.py` reader | ~150 | Nothing new | BM-11 |
| BM-14 | New converter tool | ~100-150 | Nothing new | BM-11, BM-6 |
| BM-18 | `README.md` split into `docs/deployment.md`, `docs/security.md`, `docs/tools.md`, `docs/compliance.md` | Mostly moved, not written | Nothing functional — pure docs reorg, but every external link into README anchors breaks | Nothing, do anytime |
| BM-19 | New standalone script | ~200-300 | Nothing — reads `tools/list` from any server | Nothing |

## Phase 4 — Implementation plan

**Revised sequencing**, given the Phase 2 dependency finding: BM-10 genuinely
blocks only BM-12 (scoped HTTP principals), not BM-6, BM-8, or BM-11. That
loosens the brief's rigid "BM-10 first" gate significantly.

### Milestone 0 — Already done (no action)
BM-1 (in flight, PR #10), BM-2, BM-3, BM-4. Verify PR #10 merges; nothing
else to do here.

### Milestone 1 — Measurement (BM-9 partial, BM-5)
**Goal:** every subsequent milestone has something to be measured against.
**Issues:** BM-5 (wrong-answer containment naming/docs/tests), BM-9's CI-gate
half (wire `run_eval.py --backend mock` into `.github/workflows/ci.yml`
against the *existing* 41 cases — don't wait for the 135-case expansion).
**Exit criteria:** CI fails on a router regression; README has a named
"Wrong-answer containment" section with one test per listed control.
**Escalate if:** any listed containment control does *not* already have a
test that fails without it — that's new engineering work, not docs, and
changes BM-5's size from S to at least M.

### Milestone 2 — Tool tiers (BM-8)
**Goal:** gate the KQL-authoring escape hatches, now unblocked since #5 shipped.
**Issues:** BM-8, following the already-written spec exactly.
**Exit criteria:** matches `docs/tool-tiers-implementation-spec.md`'s
acceptance section — per-lane byte-size table in the PR, `saved__*`/
`discover_schema` confirmed to stay visible in the small tier.
**Escalate if:** the measured surface reduction is much smaller than
projected (spec estimates ~12-13KB removed per operational lane) — if it's
materially less, the tier split may not be worth the added config surface.

### Milestone 3 — Discovery (BM-6, BM-7)
**Goal:** the dominant lever on small-model reliability, per the market brief.
**Issues:** BM-6, BM-7 same release.
**Exit criteria:** recall gate ≥99% at K≤5 in CI (per the market brief's own
target, tighter than the backlog's original ≥95%); no tool name in
`_BASE_INSTRUCTIONS` outside the anchor set, enforced by a test; two-hop
eval scoring in place.
**Escalate if:** recall gate can't clear ~95% after real tuning effort —
per the brief's own framing, a discovery hop below ~99% recall makes
per-question accuracy *worse* than today's single hop, which would mean
shipping BM-6 actively regresses the product.

### Milestone 4 — Router eval expansion (rest of BM-9)
**Goal:** the CI-gate infrastructure everything above is judged against, at
full coverage.
**Issues:** expand to ≥3 phrasings per small-tier tool, add lane labels
(now meaningful post-BM-8), two-hop scoring (now meaningful post-BM-6).
**Exit criteria:** published baseline in `evals/results/`, ratcheting
threshold in CI.

### Milestone 5 — Principal refactor (BM-10)
**Goal:** structural prerequisite for BM-12 only (not BM-6/8/11, per the
Phase 2 finding) — but still the right foundation before scoped HTTP auth.
**Issues:** BM-10, shipped as its own commit, zero behavioral diff.
**Exit criteria:** full existing test suite + protocol smoke pass
unmodified; grep test confirms no remaining module-level `ACTIVE_ROLE` reads
outside default-principal construction.
**Escalate if:** any existing test needs changing to pass — per the brief's
own acceptance criterion, that means the refactor changed behavior and must
be re-scoped before proceeding.

### Milestone 6 — Audit ledger (BM-11, BM-13, BM-14)
**Goal:** the clearest differentiator in the position matrix, and (per
BM-14) the path to a real eval corpus instead of synthetic cases.
**Issues:** BM-11 first (design the record schema with BM-14's future use
explicit, per the brief's own note), then BM-13, then BM-14 (needs BM-6 too).
**Exit criteria:** chain-tamper test detects mutation; property test proves
no row content in any record class; retention is configurable with no
silent default.
**Escalate if:** any design draft puts telemetry content in the ledger —
this is the one invariant the brief calls out as unrecoverable if violated.

### Milestone 7 — Scoped HTTP principals (BM-12)
**Goal:** the one item that genuinely needs BM-10's `Principal` object.
**Issues:** BM-12.
**Exit criteria:** backwards-compat test — existing single-token config
behaves identically; per-scope denial tests are ledger-recorded (needs
Milestone 6 to have landed).
**Escalate if:** the constant-time-comparison / hashed-token-storage
requirements (S5 in the review rubric) aren't met — this is the
highest-severity review category in the whole backlog.

### Deferred, not scheduled
BM-18 (README split) and BM-19 (routing reliability analyzer) — no
dependencies, no urgency, do opportunistically between milestones.

## What I could not verify from the code alone

- **Whether the discovery recall target (≥99% at K≤5) is achievable at all**
  for this tool surface — that's an empirical question Milestone 3 answers,
  not something readable from static code.
- **Whether BM-5's containment controls are actually complete** — I
  confirmed the *mechanisms* exist (`_SINCE_RE`, `_validate_user_kql`,
  schema-drift warnings) but did not exhaustively enumerate every path that
  could produce a confident false negative. Milestone 1's per-control test
  requirement is exactly the tool for finding gaps here, not this review.
- **The real cost-per-correct-answer tradeoff for BM-8's tier split** — the
  market brief (§4, "one open decision for the team") asks whether a
  single 14B tier beats the 7B/32B split outright. That's an eval-harness
  question, not something this plan can resolve; flagging it as a live
  open question rather than pretending Milestone 2 settles it.

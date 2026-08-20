# START HERE — task brief for Claude Code

You have been pointed at this directory. This file tells you where these documents came
from, what they are worth, and what you are being asked to do with them.

---

## 1. Provenance — read this before trusting anything here

**What these are.** An external competitive and architectural analysis of `berserk_mcp`,
produced by Claude (Opus) in a single long chat session on **2026-08-16**, at the request
of the repository owner. Two documents:

| File | What it is |
|---|---|
| `berserk-mcp-strategy-and-backlog.md` | Full analysis: competitive landscape, position matrix, teardowns of OpenObserve and Datadog MCP, ~25 issues (BM-1 … BM-28) across five epics, and a PR review rubric |
| `berserk-dev-brief.md` | ~10-minute summary of the same material for the dev team |

If you find a file named `berserk-mcp-pr-plan.md` anywhere, it is an **earlier,
superseded draft** covering only 5 issues. Ignore it; `berserk-mcp-strategy-and-backlog.md`
supersedes it entirely.

**How they were produced.**

- The repository was cloned and read directly. Tool counts, per-lane token estimates,
  `SIMPLE` path membership, env-var counts and eval-case counts were **measured by script,
  not estimated.**
- Market claims come from web research on 2026-08-16 — vendor documentation, published
  surveys, and academic papers. Sources are linked inline.
- Nothing here was reviewed by anyone on the Berserk team before being written down.

**Known limitations — these matter.**

1. **Line numbers are stale.** All `file:line` references were taken against `berserk_mcp.py`
   at commit `da85ac6`, then partially reconciled to `66be753` (+101 lines). References
   below `:1052` are shifted. **Grep for the named symbol. Never trust the number.**
2. **The analysis has already been wrong twice.**
   - It initially claimed no competitor attempts wrong-answer containment. False — Datadog
     does, in their experiments toolset. Corrected in §2.4 and §2.6.
   - It analysed a stale clone without checking `origin/main`, and missed that BM-2 had
     already shipped as PR #7.
   Both were caught only because someone asked. **Assume more errors remain.**
3. **The silent-failure taxonomy (SF-1…SF-5 in BM-5) is derived from code reading, not
   from observed production failures.** It is the load-bearing artifact of the entire
   reliability argument and it is currently educated guesswork.
4. **No issue here has been validated by anyone who knows the codebase.** That is your job.

**Scope constraint applied throughout:** `berserk_mcp` targets **Berserk/bzrk only**. No
multi-backend adapter. Epic D is withdrawn. See §2.5 of the main document.

---

## 2. Your task

Validate this analysis against the actual codebase and produce an implementation plan.

### Hard boundaries

- **Do not modify `berserk_mcp`.** It is reference-only for this task. You will find real
  bugs during verification and will want to fix them — file them into the plan instead.
- **Blueprint-first.** This repo (`berserk-blueprint`) is design authority. Any code change
  needs a spec here before an implementation diff lands in `berserk_mcp`.
- **Write only inside this directory** unless told otherwise.
- No installs, no network calls beyond `git fetch`, no cloud or SSH operations without
  explicit approval.

### Phase 1 — Reconcile

The analysis was written against `66be753`. Before anything else:

```
git -C <berserk_mcp path> fetch --all --tags
git -C <berserk_mcp path> log --oneline 66be753..origin/main
```

If `main` has moved, reconcile §1.2 (baseline metrics table) and §1.4 (changes since) in
the main document before proceeding. Re-run the measurement scripts implied by §1.2 rather
than trusting the numbers — they are cheap to recompute and they are the foundation of
everything else.

**One backlog item was already found closed this way.** Expect more.

### Phase 2 — Verify (adversarially)

For each issue BM-1 through BM-28, report exactly one verdict:

| Verdict | Meaning |
|---|---|
| `CONFIRMED` | Gap exists substantially as described |
| `STALE` | Already fixed — name the commit |
| `WRONG` | The analysis misread the code — explain what it actually does |
| `UNCLEAR` | Needs a human decision, not more reading |

**Be adversarial. The value of this pass is the disagreement, not the confirmation.** A
report that confirms everything is a failed report. Specifically challenge:

- Any claim about what a small model "will" do — those are predictions, not observations.
- The dependency graph. BM-10 is claimed to block BM-6, BM-8 and BM-11. Verify it.
- The sizing labels (S/M/L). They were assigned without running the code.
- Whether BM-28's decision-tree approach actually solves the composition problem it claims
  to, or just relocates it.

**Output the Phase 2 verdict table before writing anything else.** Stop there if the
reconciliation in Phase 1 invalidated a material part of the analysis.

### Phase 3 — Size

For each `CONFIRMED` issue:

- Files touched, rough LOC, test surface affected
- What it breaks (output shape, tool visibility, wire protocol, config)
- What must land first — and flag any dependency the analysis got wrong

### Phase 4 — Plan

Write `implementation-plan.md` in this directory. Sequenced, grouped into milestones, with
the ordering constraints from §3.3 of the main document respected:

1. Measurement (BM-9, BM-20, BM-21) before any behaviour change — there is no baseline
   today, so everything else would ship blind.
2. BM-10 (principal refactor) before BM-6, BM-8, BM-11.
3. BM-5 (silent-failure taxonomy) before BM-22 (capability ladder).

**Where you disagree with the sequencing, say so and argue it.** The constraints above are
reasoned but not sacred.

Include for each milestone: goal, issues, exit criteria, and what would make you stop and
escalate.

---

## 3. Context you will need

**The three-repo separation is a standing rule:**

| Repo | Role |
|---|---|
| `berserk-blueprint` | Design authority. Specs land here first. **You are here.** |
| `berserk_mcp` | Implementation |
| `berserk-knowledge` | Validated capabilities |

**Codebase constraints that apply to every proposal:**

- **stdlib only.** No third-party imports. Any proposal requiring one is wrong and should
  be re-scoped.
- Minimal diffs, UTC timestamps, stable output objects.
- No second backend (§2.5). No agent-authored code execution (§3.2). No write access to
  telemetry systems.
- At 59 tools the routing surface is the binding constraint. Any proposal that grows the
  fixed tool count needs a corresponding removal.

**The review rubric in Part 5 of the main document is for reviewing PRs, not for this
task** — but read it, because it encodes invariants your plan must not violate. The
blocking items in §5.2 (fixed-query invariants) and §5.3 (security-sensitive) are the ones
that matter most.

---

## 4. What good output looks like

- A verdict table that disagrees with the analysis in at least a few places, with reasons.
- A plan whose first milestone is measurement, because nothing else is verifiable without
  it.
- Explicit flagging of anything you could not verify from the code alone.
- No changes to `berserk_mcp`.

If you finish Phase 2 and the analysis turns out to be substantially wrong, **say that
plainly and stop.** A correct rejection is a better outcome than a plan built on a bad
premise.

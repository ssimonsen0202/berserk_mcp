# Codex review loop: what it caught in Claude-written code

Written 2026-08-18, from four merged PRs in this repo: #7, #1, #8, and the
CI failures in between. Every finding below is real. Each one came from a
Codex review of code Claude wrote and had already tested.

## The loop

1. Claude writes the change test-first. Tests fail, then pass.
2. Claude runs the full suite and the protocol smoke test.
3. Codex reviews the diff. It reports findings as P1/P2 with file:line and a
   repro command.
4. Claude verifies each finding against the code before fixing it.
5. Repeat 3 and 4 until Codex finds nothing new.
6. CI runs on the real matrix. Merge only after it passes.

Step 4 matters. Two findings this round were partly wrong, and one was right
for a reason the reviewer did not name. Read the code, run the repro, then
fix.

## What it caught

| PR | Rounds | P1 | P2 |
|---|---|---|---|
| #7 `since` normalizer | 1 | 1 | 2 |
| #1 JSON body mode | 3 | 3 | 4 |
| #8 `--doctor` preflight | 1 | 2 | 5 |

All 17 findings were in code that passed its own tests. Tests prove the code
does what the author expected. They do not prove the author expected the
right thing.

## Recurring faults

### 1. Detecting a dynamic condition from static input

PR #1 switched to JSON output when the KQL validator saw `body` in a
projection. The truncation it guarded against does not come from the query.
It comes from the calling process and its terminal width. The same query
truncated through the MCP server and did not truncate in a shell.

No amount of reading the query text can predict this. The fix was to delete
the detector and always use JSON on paths that can return `body`.

**Ask:** is the thing I am detecting a property of the thing I am reading?

### 2. Fixing one call site when the concern spans the dispatch table

PR #7 put `_normalize_since()` in `bzrk_search`. Six other paths validate
`since` before they reach that function, so the fix did not apply to them.
PR #1 changed `search` and left seven other tools that also return `body`.

**Do:** grep for every caller and every sibling path before you call a fix
complete. Write one test per path, not one test for the path you edited.

### 3. Inventing a data shape instead of reading the real one

PR #1 matched `_score` with a regex built for `{"_score": 0.83}`. Real
`bzrk --json` returns Kusto shape: column names in `schema`, values in
`rows`, never adjacent. The regex found the name, found no number, and
reported semantic search as unavailable when it worked.

The repo already had `agent_analytics._json_records`, written for this exact
shape, with a test that cites the date it was confirmed against live output.

The same fault appeared in PR #8. The `recent_rows` check read
`rows_returned` from `--stats`. That field counts response rows. A `| count`
query returns one row, so it always read 1, never the count.

**Do:** run the real command and read the real output. Then check whether the
repo already parses it.

### 4. Required checks that fail open

PR #8's `--doctor` accepted any exit-zero output as a valid version, and
treated a missing count field as a pass. With `BZRK_BIN=/usr/bin/true`, every
required check passed and the exit code was 0.

A check exists to detect breakage. If it cannot tell "healthy" from "no
evidence", it reports health for both.

**Do:** for a required check, make absence of evidence a failure.

### 5. A checker that can crash

`--doctor` ran ten checks in a list. A malformed response made one raise
`AttributeError`, which killed the whole report. One bad check destroyed the
other nine.

**Do:** isolate each check. A diagnostic tool must survive the faults it
looks for.

### 6. Tests that reach real machine state

PR #8's tests mocked `run_bzrk`. They did not mock `shutil.which`, which one
check calls directly, or `parser_factory._llm_config`, which reads a real
file on disk. `bzrk` is on the dev machine's PATH and not on the CI runner's,
so the tests passed here and failed there.

**Do:** when a test drives an aggregate, mock every dependency the aggregate
can reach, not just the ones its own name suggests. Then run it under the
conditions CI has: `PATH=/usr/bin:/bin python3 tests/...`.

### 7. Chasing the repro instead of the class

`_JSON_UNSUPPORTED_RE` took three rounds. Each fix matched the exact string
in the last report, and each time Codex found another string that slipped
through. The regex matched on vocabulary: "invalid", "unexpected", "unknown".

The fix that held matched on structure. Every real clap argument error puts
the word `argument` next to the flag and appends a usage trailer. An
unrelated error does not do both.

**Do:** when a second fix to the same pattern fails, stop patching. Find the
structural property.

### 8. POSIX assumptions in test fixtures

Two Windows-only failures: a hardcoded `/usr/local/bin/bzrk` is not absolute
on Windows, and `chmod(0o500)` does not stop Windows creating a directory.

**Do:** build paths from `tempfile`. Guard permission tests with
`skipIf(os.name == "nt")`.

### 9. Claiming a check you did not run

The PR body for #8 said the suites pass. They did on the dev machine. CI
failed on all six jobs, and Codex found it before Claude did.

This is the worst fault on the list. The others are mistakes. This one is a
false report.

**Do:** run `gh pr checks <n>` before you write that CI passes. State what you
ran, not what you expect.

### 10. A review that verified by reading, not running

The model-behavior monitoring branch (issues #89-#92) went through 6
independent task reviews plus a final whole-branch review, all clean. Codex
found `_attach_fingerprints()` — the function wiring the canary and the
fingerprint module together for the first time — raised `NameError` on
every real call. It referenced a module-global `fingerprint` that only
existed as a name local to a different function's scope.

Every review read the function and reported it correct. The final review's
own report said "`_attach_fingerprints()` exists, is actually called from
`--canary-run`" as a checked, verified line. None of that was false as
stated — the function does exist, and `--canary-run` does call it. What no
review did was execute it. `grep` across the whole test suite for the
function's name returned nothing, despite the task's own brief explicitly
asking for a mocked test. A NameError fires on the first call regardless of
how carefully the surrounding code was read; only running it surfaces that.

The same review pass, done independently by Codex after the branch merged,
also found a new CLI branch with no `return`/`sys.exit()` at its end —
execution fell through into a blocking server start, turning a one-shot
cron command into a hang. Reading the new hunk in isolation cannot catch
this; it only shows up tracing the whole containing function to its end.

**Do:** for any function whose job is to connect two modules for the first
time, grep the test suite for its name before approving the task. If
nothing calls it, that is the finding. Then invoke it yourself — a bare
`python3 -c "import x; x.fn(...)"` catches `NameError`/`ImportError` in one
line, no mocking required for that class of bug. For any new branch in an
existing dispatch/control-flow function, trace execution to the end of the
function it was inserted into, not just to the end of the diff hunk.

## What the loop does not do

Codex reads the diff and the code around it. It does not know what the change
is for. It reported the `--doctor` timeout as a defect because five seconds
is per-socket, not wall-clock — correct, and true of every HTTP call in the
repo, not this diff. Scope is the author's job.

Two findings needed correction on inspection. Verify before you fix.

## Running a review

Use the `codex:codex-rescue` subagent. Give it:

- repo, branch, head commit, base commit
- what the change does, in structural terms
- the specific questions you want answered
- the report format: P1/P2, file:line, a repro command

Pass `--fresh` unless you are continuing a review of the same code. A stale
job can sit in `verifying` for hours; check it with
`codex-companion.mjs status <id>` and cancel it rather than waiting.

Tell it the current commit if an earlier review ran against an older one.
Codex will otherwise report findings you already fixed.

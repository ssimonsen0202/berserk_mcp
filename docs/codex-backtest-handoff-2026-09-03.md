# Codex backtest handoff — resume when quota resets

**Codex hit its usage limit mid-run on 2026-09-02** while backtesting commit
`35750fc` (the second fix pass, addressing the first backtest's findings
against `dbcd7d4`). It reset at **2026-09-03 02:00 AM**. This note is what to
hand Codex to pick the backtest back up without re-deriving context.

## Resume instructions

Resume the existing thread rather than starting fresh — it already has the
history of both prior reviews:

```
/codex:rescue --resume Backtest commit 35750fc on the berserk-mcp-server
repo (main branch, right after dbcd7d4 which you already reviewed and
backtested). See docs/codex-backtest-handoff-2026-09-03.md for what to check.
```

Thread ID (if `--resume` needs it explicitly): `01a0637e-3f54-7bd1-93cf-9f7551065c5c`
Prior task ID: `task-mtkid8tu-7wmv4j`

## What it had already done before hitting the limit

Orientation only — no verdicts were produced. It had read `berserk_mcp.py`,
`model_drift.py`, and `evals/canary.py`, diffed `dbcd7d4..35750fc`, and
grepped for `BzrkResultParseError`, `run_drift_report`, and the fingerprint
logic. None of that work needs to be redone; it can go straight to
verification.

## What to verify in `35750fc`

This commit is the fix for everything the **first** backtest (against
`dbcd7d4`) found. Check each item against the *current* code, not the diff
alone:

1. **Fail-open regression** — `group_by_model()` in `model_drift.py` now
   raises `BzrkResultParseError` on non-JSON bzrk output instead of silently
   returning `{}`. Confirm `model_drift_check`, `model_drift_history` (both
   in `berserk_mcp.py`), and the new standalone `run_drift_report()` (also
   `berserk_mcp.py`) all catch it explicitly and report failure — not just
   that the exception type exists.

2. **Fingerprint-boundary miss** — `_fingerprint_changed()` in
   `model_drift.py` used to compare only the two most recent rows, missing a
   transition landing exactly at the baseline/recent split. It's now derived
   from the same `baseline_rows`/`recent_rows` split `classify()` computes
   for its accuracy comparison. Confirm both signals actually read from the
   same window (not just that the function signature changed).

3. **Missing fingerprint-status rendering** — both `model_drift_check` and
   `model_drift_history` dispatcher branches in `berserk_mcp.py` promised
   fingerprint status in their tool descriptions but never rendered it.
   Confirm both now append fingerprint info to their output text.

4. **Missing `BERSERK_MCP_TIER` in cohort key** — `_current_environment_rows`
   in `model_drift.py` added `tier` alongside the existing `role`/
   `discovery_mode` fields. `evals/canary.py`'s `_current_environment()`,
   `build_eval_record()`, and `build_failure_record()` were updated to
   record `eval.tier` the same way they record `eval.role`/
   `eval.discovery_mode`. Confirm the tier value flows end-to-end (recorded
   at eval time, read back at classify time) and is in
   `EVAL_ATTRIBUTE_ALLOWLIST`.

5. **Confidence-vs-alerting under low `repeats`** — deliberately left
   as-is (not a code change): `--drift-report` alerts regardless of
   confidence, but confidence is shown in the alert text so an operator can
   triage. Confirm this reasoning still holds against the current code
   rather than re-flagging it as unfixed.

Report using the same format as before: FIXED / PARTIALLY FIXED / NOT FIXED
/ REGRESSION / NEW FINDING per item, plus anything new the second fix pass
itself introduced.

## What's already independently verified (context, not a substitute for the backtest)

- Every fix was proven by direct execution (ad-hoc repro scripts matching
  Codex's own counterexamples) before being written up as permanent tests —
  not just by passing pre-existing tests in isolation.
- Full test suite is green: 966 tests in `tests/`, 130 in `evals/`.
- New coverage: `tests/test_model_drift.py` (28 tests), `evals/test_canary.py`
  (14 tests), and new `RunDriftReportTest` /
  `ModelDriftDispatcherFingerprintTest` classes in `tests/test_berserk_mcp.py`.
- Already pushed to `origin/main` (GitHub) and `gitea/main` as `35750fc`.

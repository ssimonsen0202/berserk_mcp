# Learning loop: decision log

One row per work package of the learning-loop work. The supervisor adds the
row when the package merges.

## Approval

| Date | Decision |
|---|---|
| 2026-11-16 | The owner approved release A (WP0, WP1, WP3, WP5, WP6) of the implementation spec, revision 4. Later releases need their own approval. |
| 2026-11-16 | `save_query` rejects a query that returns no rows by default; `allow_empty=true` lets one through. |
| 2026-11-16 | One patch version and one release note per package. |

## Baseline (WP0)

Commit `e0ccea3` (v1.37.5), Python 3.14.7, macOS, non-root user.

| Gate | Result |
|---|---|
| `python3 -m unittest discover -s tests` | 1,437 tests, 1 skipped, 0 failures |
| `python3 -m unittest discover -s evals -p 'test_*.py'` | 215 tests, OK |
| `python3 -m unittest discover -s ingestion -p 'test_*.py'` | 23 tests, OK |
| `make lint-ci`, `make format-check`, `make typecheck` | clean |
| `make coverage-ci` | 86% (gate 80%) |

Checked in code: an empty result arrives as the `(no rows)` sentinel with
`--json` too, because `runner._run_bzrk_launch` maps empty stdout to it.
Not checked live: whether each MCP client honours
`notifications/tools/list_changed`.

## Packages

| Package | Version | PR | Deviations | Open follow-ups |
|---|---|---|---|---|
| WP0 baseline | v1.37.6 | pending | The fixture's two generated entries went through `persist_learned_query` with a minimal entry, so they lack the validation fields the full generation pipeline adds. | Add an entry with no `status` field (an older store shape) to the fixture. The generated path treats a table of several rows ending in `0` as empty (`count_result_is_zero`); fix after WP1. |

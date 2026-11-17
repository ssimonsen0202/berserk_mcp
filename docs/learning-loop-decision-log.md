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
| WP0 baseline | v1.37.6 | [#104](https://github.com/ssimonsen0202/berserk_mcp/pull/104) | The fixture's two generated entries went through `persist_learned_query` with a minimal entry, so they lack the validation fields the full generation pipeline adds. Codex review round 1 (2 low findings, both fixed): two older store shapes (a generated entry with no `status`, one with `generated_by` but no `origin`) are added by hand in the compat test, since v1.37.5 cannot write them; `saved__*` dispatch now asserts the stored KQL and `since`. | The generated path treats a table of several rows ending in `0` as empty (`count_result_is_zero`); fix after WP1. |
| Fix outside the spec: fence every generated description | v1.37.7 | [#105](https://github.com/ssimonsen0202/berserk_mcp/pull/105) | Found by the WP0 Codex review, round 2; the owner asked for its own PR. Codex round 1 widened it to `list_saved` forged tags and `review_generated`; round 2 found none. Eviction and the `_gen` rename still test `origin` only, on purpose. | none |
| WP1 save_query rows, fingerprint | v1.37.8 | [#106](https://github.com/ssimonsen0202/berserk_mcp/pull/106) | Added two helpers the spec does not name: `entry_fingerprint` (applies the `kql_fp_v` rule) and `entry_rows_verified` (needed by the legacy test). `_empty_result_check` and `_visible_duplicate` were extracted to keep `save_query` under the complexity limit. Existing test stubs returned a header-less `"OK"`, which now counts as zero rows; they return `"col\nOK"`. Cap and concurrency tests reused one KQL for many entries; they now vary it. Tests-first proof: 24 of 25 new tests failed on the base commit; the 25th was tightened to fail too. Codex round 1: one finding outside WP1 (save_query's same-name check ignores visibility), handled separately. Round 2: the job report named a duplicate hidden from the role; `persist_learned_query` now also returns `duplicate_visible`, and the report shows `(hidden)` (one extra key beyond the spec's return shape). A `nosemgrep` marker documents why the empty-result error return is fenced. | A text result of one header-less line counts as zero rows; `bzrk --json` output is never in that form. |
| Fix outside the spec: `save_query` and hidden names | v1.37.9 | [#107](https://github.com/ssimonsen0202/berserk_mcp/pull/107) | Found by the WP1 Codex review, round 1; the owner asked for its own PR. The store write also refuses under its lock (`HiddenNameError`). Codex round 1 widened it: refuse before any query runs; generated writes and `review_generated` respect visibility. Accepted limit: a taken name can be told apart from a free one (store-wide names). | Per-role name spaces, if roles must be a secrecy boundary (owner decision). At the 500-entry cap, eviction can remove an entry another role owns (Codex round 2); take up in WP4, which rewrites eviction. A hidden name created in the gap between the check and the save still costs the verification queries before the refusal (Codex round 3); accepted, nothing is replaced or revealed. |
| WP3 saved-query envelope | v1.37.10 | [#108](https://github.com/ssimonsen0202/berserk_mcp/pull/108) | `review=` is `approved` or `pending` only; `unreviewed` arrives with WP8. The "last returned rows at" sentence waits for WP4. Two helpers added (`_saved_header_extra`, `_empty_saved_text`). A `nosemgrep` marker documents why the header (counts, a sanitised name and a digest) may be returned. Tests-first proof: all 9 new tests failed on the base commit. Codex round 1 (2 low, both fixed): `since` is stripped before it reaches the header; `run_saved` now resolves a name the same way as `saved__*` dispatch (an unsanitised stored name used to run only through `saved__*`). Round 2 (2 low): the SIMPLE tools put an unstripped `since` into the same header; `_envelope_header` now strips it for every caller. Round 3 (1 low): whitespace inside `since` (tab, vertical tab, U+2028) also reached the header; `tools.display_since` now collapses every whitespace run, for both paths. Two stored names that sanitise to the same value still collide (predates this work). | Reject or repair stored names that sanitise to the same value (`foo-bar` and `foo bar` both become `saved__foo_bar`). |
| WP5 real model id | v1.37.11 | [#109](https://github.com/ssimonsen0202/berserk_mcp/pull/109) | `_provider_model` returns an error for an unknown provider (the spec names three). A failed model lookup in the ladder counts as an attempt, like any other failed call. Three `spy_llm_complete` test doubles gained `model=None`. Tests-first proof: all 5 new tests failed on the base commit. Codex round 1 (1 finding, declined): an operator-set model id or a Hermes-reported id such as "unknown" is recorded as sent; the record must say what was sent, and the code itself no longer writes a fallback. | none |
| WP6 ladder failure history | v1.37.12 | [#110](https://github.com/ssimonsen0202/berserk_mcp/pull/110) | A provider that generated a reply and then hit a call error still adds a note from its last validation or parse errors. One extra test beyond the spec (a model-lookup error adds no note). Secret and forged-tag tests patch `_parse_generated_reply`, because real parse errors do not echo model text. Tests-first proof: all 6 new tests failed on the base commit. | none |

## Release A: complete (2026-11-17)

Release A of the spec (revision 4) is merged: WP0, WP1, WP3, WP5 and WP6, as
v1.37.6, v1.37.8, v1.37.10, v1.37.11 and v1.37.12. Each package has its own
release note in `docs/releases/`. Codex reviews of these packages found two
defects outside the spec, each fixed in its own PR (v1.37.7 and v1.37.9).

What release A changes for users:

- `save_query` refuses a query that returns no rows in 24 hours, unless
  `allow_empty=true`, and refuses the same KQL under a second name.
- `save_query` cannot reveal or replace a saved query the caller cannot see.
- Saved-query results carry the same evidence header as the built-in tools.
- Generated queries record the model that wrote them, and a later model in
  the ladder sees what earlier models got wrong.

Open follow-ups, in the rows above: stored names that sanitise to the same
value collide; eviction at the 500-entry cap ignores roles (WP4); the
generated path's count check misreads a several-row table ending in `0`.

Release B (WP2, WP4, WP17 report) needs its own owner approval.

## Release B

Approved by the owner on 2026-11-17: WP2 and WP4. WP17 moves to release C,
where the request store it reports on is built. D3: discovery mode lists up to
5 saved tools. WP4 eviction removes only entries the writing role and tier can
see.

| Package | Version | PR | Deviations | Open follow-ups |
|---|---|---|---|---|
| WP2 saved tools findable | v1.37.13 | pending | Supervisor decision: word weights for every entry come from the built-in tools only (`tool_discovery.search(..., weights_from=...)`). With shared weights, 500 saved entries dropped built-in recall from 0.991 to 0.986 ("what containers are running" missed); fixed weights keep the baseline and stop a large or stuffed store from diluting built-in tools. Index cache key adds role and tier. Item 7 (request_query pointer) waits for WP7; item 5 uses most-recent order until WP4. Tests-first proof: all 24 new tests failed on the base commit. Codex round 1 (3 low, all fixed): the index cache key adds inode and ctime; `list_saved` shows sanitised names; the index holds at most 500 entries. | none |

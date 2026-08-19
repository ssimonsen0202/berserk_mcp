# Implementation specification: result envelope for the fixed-query path

Tracks GitHub issue [#2](https://github.com/ssimonsen0202/berserk_mcp/issues/2).
Written 2026-08-18 against commit `4e0172b`.

**Re-verify every line number before you edit.** Names are stable; line
numbers drift.

Independent of issues #4 and #5. Can ship in any order relative to them.

## Purpose

24 tools dispatch through `SIMPLE` (`:1695`) and return `bzrk` stdout
verbatim (`:2349`):

```python
if name in SIMPLE:
    kql, default_since = SIMPLE[name]
    since = arguments.get("since") or default_since
    if name in _SIMPLE_JSON_TOOLS:
        return bzrk_search_json(kql, since)
    return bzrk_search(kql, since)
```

These are the highest-traffic tools in the server. Two problems.

### The window is invisible

Default windows differ per tool and are never echoed back:

| Default | Tools |
|---|---|
| `15m ago` | `list_containers`, `top_cpu`, `top_memory` |
| `30m ago` | `host_cpu`, `host_memory`, `sre_host_headroom` |
| `1h ago` | `errors_by_service`, `list_services`, `list_hosts`, `container_hosts`, `list_metrics`, `bzrk_query_perf`, `sre_error_rate`, `sre_ingest_health`, `sre_top_error_messages`, `soc_high_severity_logs`, `soc_log_spike`, `claude_recent`, `trace_find_slow`, `trace_find_errors` |
| `6h ago` | `soc_repeated_errors`, `claude_sessions`, `claude_tools`, `claude_errors` |

A model that omits `since` gets rows with no idea what span they cover, then
writes "there were 12 errors" — a wrong sentence built on a correct query.
`errors_by_service` and `soc_repeated_errors` differ by 6x and the model
cannot tell from the response.

### An empty result is a bare sentinel

`run_bzrk` returns the literal string `(no rows)` on empty stdout
(`:1051`, `return (out or "(no rows)"), False`).

For a small model that string is ambiguous between four different situations:

1. Healthy — nothing is wrong, correctly nothing to report.
2. Wrong window — the events exist outside the default span.
3. Wrong tool — `top_cpu` asked when the question was about hosts.
4. The source stopped reporting — a real incident, reported as silence.

In practice it produces confident false negatives, or blind retries.

### The pattern already exists

`detect_anomalies` (`:2266`) and `forecast_capacity` (`:2278`) already do
this correctly:

```python
if not out or out.strip() == "(no rows)":
    return f"No anomalies detected (window {since}).", False
return f"Anomaly decomposition for window {since}; non-zero anomaly markers indicate spikes:\n{out}", False
```

They name the window and interpret the empty case. Apply the same treatment
to the 24 tools called most.

### Related: the overflow message misdirects on this path

`run_bzrk` returns (`:1041`):

> bzrk result exceeded BERSERK_MCP_MAX_RESULT_BYTES=…; narrow the time
> window, project fewer columns, or add a smaller take/top/tail bound.

On a `SIMPLE` tool the query is fixed. The model cannot project fewer columns
or add a bound. Its only lever is `since`. Two thirds of that advice is
unfollowable, and a model that tries will fail and retry.

## Current architecture to preserve

Stdlib only. No new imports.

- `SIMPLE` (`:1695`) maps tool name to `(kql, default_since)`.
- `_SIMPLE_JSON_TOOLS` (`:1732`) is the four body-bearing tools that must use
  `--json`. Do not change that set here; see
  `docs/claude-code-review-feedback-loop.md` fault 1 for why it exists.
- `run_bzrk` (`:1027`) owns the `(no rows)` sentinel and the overflow message.
- `handle_call` wraps dispatch with cache, budget, and fail cooldown. The
  cache stores the returned text, so an envelope is cached with its result —
  which is correct, since the window is part of the key.

## Functional requirements

### FR-1 Envelope the `SIMPLE` path only

```python
def _envelope(tool, since, out):
    """Header naming the resolved window and row count, then the raw rows."""
```

Applied **only** in the `if name in SIMPLE:` branch. Do not apply it to
`search`, `run_saved`, the analytics tools, or anything that already
interprets its own output. Those either return JSON that a caller parses, or
already name their window.

Output shape:

```
window=1h ago  rows=12

<raw bzrk output, byte-identical>
```

Requirements:

1. The raw rows appear verbatim below the header. Anything downstream that
   greps or parses the output keeps working. Do not reformat, re-align, or
   truncate the rows.
2. `rows=` counts data rows, not lines. Derive it without a second query.
   For table output that means total lines minus the header line; for
   `--json` output, use `agent_analytics._json_records` and take `len`.
   Read `docs/claude-code-review-feedback-loop.md` fault 3 before writing a
   counter — the wrong field has been picked twice in this codebase.
3. If the row count cannot be determined, omit `rows=` rather than guessing
   or printing 0. A wrong count is worse than a missing one.
4. Never raise. A failure to build the envelope must return the raw output
   unchanged, not an error.

### FR-2 Interpret the empty case with a per-tool next step

For `(no rows)`, return a sentence naming the window and one concrete next
step. Use a static table. No LLM, no extra query, no inference.

```python
_EMPTY_NEXT_STEP = {
    "errors_by_service":
        "Widen with since='24h ago', or confirm the source is reporting with list_services.",
    "top_cpu":
        "For whole-machine CPU use host_cpu; top_cpu is per-container.",
    "host_cpu":
        "For per-container CPU use top_cpu; host_cpu is per-host.",
    # ... one entry per SIMPLE tool
}
```

Rendered as:

```
No rows in window 1h ago. Widen with since='24h ago', or confirm the source
is reporting with list_services.
```

Requirements:

1. One entry per `SIMPLE` tool. Add a test that asserts
   `set(_EMPTY_NEXT_STEP) == set(SIMPLE)` so a tool added later cannot ship
   without a next step.
2. Each next step names a real tool or a real argument. Do not write "check
   your configuration".
3. Point the confusable pairs at each other: `top_cpu`↔`host_cpu`,
   `top_memory`↔`host_memory`. That disambiguation currently lives only in
   the tool descriptions, where it is read before the call, not after a
   confusing empty result.
4. `is_error` stays `False`. An empty result is a valid answer, not a
   failure. It must not trip fail cooldown.

### FR-3 Fix the overflow message on the fixed-query path

When a `SIMPLE` tool overflows, the message must name `since` as the only
lever:

```
Result exceeded BERSERK_MCP_MAX_RESULT_BYTES=10485760. This tool's query is
fixed — narrow the window, e.g. since='15m ago'.
```

`run_bzrk` does not know which tool called it, so either pass the context
down or map the message in the `SIMPLE` branch. Mapping in the branch is
smaller and touches less; prefer it unless you find a reason not to.

Leave the existing message unchanged for `search` and other free-KQL paths,
where all three suggestions are followable.

### FR-4 Gate it for one release

```python
ENVELOPE_ENABLED = os.environ.get("BERSERK_MCP_ENVELOPE", "1").strip().lower() not in {"0", "false", "no", "off"}
```

Default on. `BERSERK_MCP_ENVELOPE=0` restores byte-identical prior output for
one release cycle, so an operator with downstream parsing has an escape.

Document it in `.env.example` under "Server behavior", or
`EnvExampleDriftTest` fails the build.

## Non-goals

- No change to the 24 KQL strings. `test_locked_query_strings` guards them
  deliberately.
- No change to `_SIMPLE_JSON_TOOLS`.
- No envelope on `search`, `run_saved`, `saved__*`, the `claude_*` analytics
  reports, or the CanonLoom bridge.
- No new tools.
- No LLM call anywhere in this path. The next-step table is static. This is
  the fixed-query path; keeping it deterministic is the point.

## Test plan

Add `ResultEnvelopeTest` to `tests/test_berserk_mcp.py`. Monkeypatch
`bm.run_bzrk`, per the existing convention.

Write each test first and watch it fail for the right reason.

Required cases:

1. Rows present: header names the resolved default window, `rows=` is
   correct, and the raw output appears verbatim below it.
2. An explicit `since` argument appears in the header, not the default.
3. `(no rows)`: returns the interpreted sentence with the window and the
   tool's next step, `is_error=False`.
4. `set(_EMPTY_NEXT_STEP) == set(SIMPLE)` — no tool without a next step.
5. Overflow on a `SIMPLE` tool returns the `since`-only message.
6. Overflow on `search` returns the original three-lever message unchanged.
7. `BERSERK_MCP_ENVELOPE=0` returns output byte-identical to the current
   behavior, for both a populated and an empty result.
8. A `_SIMPLE_JSON_TOOLS` member (`claude_errors`) still passes `--json`
   **and** gets an envelope, with the row count read from the JSON shape.
9. An auth failure still returns `AUTH_FAILURE_MESSAGE` with
   `is_error=True`, unenveloped — the envelope must not wrap error paths.
10. Envelope construction failing on malformed output returns the raw output
    rather than raising (FR-1.4).

Existing tests that must pass unchanged with the gate off:
`tests/test_berserk_mcp.py`, `evals/mcp_protocol_smoke.py`. Several assert on
exact `SIMPLE` tool output; run them with the gate both on and off, and
update any that assert raw output only after confirming the change is the
envelope and not a regression.

### Eval work

Add router or answer cases where the pass condition is "the model does not
assert a false negative given a `(no rows)` response" — the failure this
change exists to prevent.

## Acceptance

Run, and paste real output into the PR:

```bash
python3 tests/test_berserk_mcp.py
python3 -m unittest discover -s tests
python3 evals/mcp_protocol_smoke.py --include-http
BERSERK_MCP_ENVELOPE=0 python3 -m unittest discover -s tests
```

The last one matters: it proves the escape hatch actually restores prior
behavior.

Then, before writing that CI passes:

```bash
gh pr checks <n> --repo ssimonsen0202/berserk_mcp
```

Include one real before/after example in the PR — an actual `top_cpu` empty
result and an actual populated one, copied from a live run, not composed by
hand.

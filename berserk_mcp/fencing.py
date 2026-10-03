"""Wrap real telemetry as untrusted data and cap what reaches the model.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
import _tag_guard
import json


def _fence_untrusted(text, inline=False):
    """Wrap real telemetry content in an explicit untrusted-data marker.

    Skips fencing for known-safe sentinels -- the empty-result marker, the
    fixed auth-failure message, and the overflow message -- rather than
    real bzrk output, checked by exact content so this never depends on
    the caller correctly tracking an err flag (round 2 finding 4: err=True
    does not always mean "no real content", so callers should fence
    unconditionally and let this function make the actual decision).

    inline=True omits the surrounding newlines, for content embedded
    inside a single report line rather than standing alone (agent_analytics
    snippet embedding, issue #11 round 2 finding 1).
    """
    stripped = str(text).strip()
    if (
        stripped == "(no rows)"
        or stripped == bm_config.AUTH_FAILURE_MESSAGE
        or bool(bm_config._OVERFLOW_SENTINEL_RE.fullmatch(stripped))
    ):
        return text
    body = _tag_guard.neutralize(text, bm_config._UNTRUSTED_DATA_TAG_RE, "untrusted_log_data")
    sep = "" if inline else "\n"
    return f"{bm_config._UNTRUSTED_DATA_OPEN}{sep}{body}{sep}{bm_config._UNTRUSTED_DATA_CLOSE}"


def _compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _cut_rows(doc, rows, budget):
    """Trim `rows` (a list inside `doc`) in place to the leading rows whose
    compact JSON fits in `budget` characters. Sizes each row once instead of
    re-serializing the whole document per row, since the input can be up to
    MAX_BZRK_RESULT_BYTES. Returns (kept, total), or None with `rows` left
    unchanged when even the document without rows doesn't fit."""
    source = list(rows)
    rows[:] = []
    size = len(_compact(doc))
    if size > budget:
        rows[:] = source
        return None
    kept = 0
    for row in source:
        size += len(_compact(row)) + (1 if kept else 0)
        if size > budget:
            break
        kept += 1
    rows[:] = source[:kept]
    return kept, len(source)


def _cut_text(text, budget, reason=""):
    """Leading characters of `text` within `budget`, ending at the last line
    break that fits. Uses rfind on the budget window only, never splitlines
    on the whole text (Codex Security finding 2: ~71 MB for 10 MB of short
    lines)."""
    end = text.rfind("\n", 0, budget + 1)
    cut = text[:end].rstrip("\r") if end > 0 else text[:budget]
    note = (
        f"[berserk-mcp: result truncated{reason}, showing the first {len(cut)} of {len(text)} characters "
        f"(BERSERK_MCP_MAX_OUTPUT_CHARS). {bm_config._TRUNCATION_HINT}]"
    )
    return cut, note


def _limit_model_output(out, budget, parse_json=True):
    """Cut a user-KQL result (search, saved queries) to `budget` characters
    before it is fenced, so the fence stays intact and the returned note is
    server text the caller appends outside it. Returns (out, note); output
    within budget comes back untouched with an empty note.

    bzrk --json results ({"Tables": [{"schema": ..., "rows": [[...]]}]}) and
    bare JSON arrays up to _MAX_JSON_PARSE_CHARS are cut by whole rows and
    stay valid JSON. Larger JSON, table text from a bzrk without --json, and
    unknown shapes are cut as text at a line boundary. parse_json=False
    forces the text cut (used when no query slot is free)."""
    text = str(out)
    if not budget or len(text) <= budget:
        return out, ""
    if len(text) > bm_config._MAX_JSON_PARSE_CHARS and text.lstrip()[:1] in ("{", "["):
        return _cut_text(text, budget, reason=" (too large to cut by rows)")
    doc = None
    if parse_json:
        try:
            doc = json.loads(text)
        except (json.JSONDecodeError, TypeError, RecursionError):
            doc = None
    rows = None
    if isinstance(doc, list):
        rows = doc
    elif isinstance(doc, dict):
        tables = doc.get("Tables")
        if (
            isinstance(tables, list)
            and tables
            and isinstance(tables[0], dict)
            and isinstance(tables[0].get("rows"), list)
        ):
            rows = tables[0]["rows"]
    if rows is not None:
        cut = _cut_rows(doc, rows, budget)
        if cut is not None:
            kept, total = cut
            note = (
                f"[berserk-mcp: result truncated, showing {kept} of {total} rows to stay within "
                f"{budget} characters (BERSERK_MCP_MAX_OUTPUT_CHARS). {bm_config._TRUNCATION_HINT}]"
            )
            return _compact(doc), note
    return _cut_text(text, budget)


def _fence_limited(out):
    """Fence a user-KQL result after cutting it to MAX_OUTPUT_CHARS. The
    truncation note is server text built only from counts, so it goes after
    the closing fence tag. Listed as a sanitizer in
    .semgrep/fence-untrusted-data.yml because it always calls
    _fence_untrusted on the content.

    The query slot is released when bzrk exits, so it does not bound this
    step on its own (Codex Security finding 1). An over-budget result is cut
    while holding a slot again; if none frees up in time, the cut falls back
    to the text path, which needs no decoding."""
    limited, note = out, ""
    if bm_config.MAX_OUTPUT_CHARS and len(str(out)) > bm_config.MAX_OUTPUT_CHARS:
        acquired = bm_config._query_semaphore_acquire(bm_config._POST_PROCESS_SLOT_WAIT_SECONDS)
        try:
            limited, note = _limit_model_output(out, bm_config.MAX_OUTPUT_CHARS, parse_json=acquired)
        finally:
            bm_config._query_semaphore_release(acquired)
    fenced = _fence_untrusted(limited)
    return f"{fenced}\n{note}" if note else fenced


def _wrap_analytics(result):
    """Fence the text of an analytics error result.

    Analytics functions return raw bzrk_search output on the error path,
    which can include partial stdout rows interleaved with the error message.
    SUCCESS output is already fenced via the fence= dependency injection;
    this covers only the error path.
    """
    text, is_err = result
    return (_fence_untrusted(text) if is_err else text), is_err

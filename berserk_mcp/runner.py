"""Run bzrk: bounded subprocesses, search, schema and KQL validation.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import queries as bm_queries
from pathlib import Path
import _kql_boundary
import contextlib
import kql_validation
import re
import schema_registry
import subprocess
import threading
import time


# ---------- bzrk invocation ----------
# bzrk has been observed to print an authentication failure (e.g. "Refresh
# token rejected...") to stderr while still exiting 0 -- a real 2026-07-10
# incident on the bzrk-q bash wrapper, which already carries this same guard
# (_bzrk_check_auth). This Python adapter never got the equivalent fix, so an
# exit-0 auth failure was silently returned as a successful empty result
# (confirmed by the 2026-07-18 security review, SEC-003). Match bzrk-q's
# pattern exactly for consistency between the two wrappers.
_AUTH_FAILURE_RE = re.compile(
    r"refresh token rejected|run .{0,200}bzrk login|unauthorized|unauthenticated|"
    r"login required",
    re.IGNORECASE,
)


# The same pattern over raw stderr bytes, applied to the whole stream while it
# is read, not only to the retained diagnostic prefix: a marker after
# MAX_BZRK_DIAGNOSTIC_CHARS must still be classified (Codex Security scan
# 77004e6e finding 3). Every alternative is bounded (at most ~220 bytes), so a
# rolling overlap of _AUTH_SCAN_OVERLAP bytes finds a match split across reads.
_AUTH_FAILURE_BYTES_RE = re.compile(_AUTH_FAILURE_RE.pattern.encode("ascii"), re.IGNORECASE)


_AUTH_SCAN_OVERLAP = 512


# F-005/SR-17: bound both diagnostics and successful output while the child
# is still running. Row limits do not bound wide rows, and capture_output
# buffers an entire stream before this process can inspect it.
MAX_BZRK_DIAGNOSTIC_CHARS = 100_000


_PROCESS_READ_CHUNK = 64 * 1024


def _run_argv_bounded(
    argv,
    timeout,
    stdout_cap=bm_config.MAX_BZRK_RESULT_BYTES,
    stderr_cap=MAX_BZRK_DIAGNOSTIC_CHARS,
    stderr_watch=_AUTH_FAILURE_BYTES_RE,
):
    """Run argv without a shell, bounding captured bytes before decoding.

    Two readers drain stdout and stderr concurrently to avoid pipe deadlocks.
    stdout overflow terminates and reaps the child; stderr is retained only up
    to its diagnostic cap while the remainder is discarded until completion.
    Every stderr byte, retained or not, is searched for `stderr_watch`
    ("stderr_watch_matched"). "streams_complete" is False when a reader was
    still running after the child exited, so its stream may be incomplete.
    """
    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {"stdout": max(1, int(stdout_cap)), "stderr": max(1, int(stderr_cap))}
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    stderr_watch_matched = threading.Event()
    reader_errors = []

    def drain(name, stream):
        tail = b""
        try:
            with stream:
                while True:
                    chunk = stream.read(_PROCESS_READ_CHUNK)
                    if not chunk:
                        break
                    if name == "stderr" and stderr_watch is not None and not stderr_watch_matched.is_set():
                        window = tail + chunk
                        if stderr_watch.search(window):
                            stderr_watch_matched.set()
                        tail = window[-_AUTH_SCAN_OVERLAP:]
                    remaining = caps[name] - len(buffers[name])
                    if remaining > 0:
                        buffers[name].extend(chunk[:remaining])
                    if len(chunk) > max(0, remaining):
                        if name == "stdout":
                            stdout_overflow.set()
                        else:
                            stderr_overflow.set()
        except Exception as exc:  # pragma: no cover - defensive OS pipe failure
            reader_errors.append(exc)

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + max(0.0, float(timeout))
    timed_out = False
    while process.poll() is None:
        if stdout_overflow.is_set():
            try:
                process.kill()
            except OSError:  # pragma: no cover - child exited between poll and kill
                pass
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            try:
                process.kill()
            except OSError:  # pragma: no cover - child exited between poll and kill
                pass
            break
        stdout_overflow.wait(min(0.05, remaining))
    process.wait()
    for thread in threads:
        thread.join(timeout=2)
    if reader_errors:
        raise reader_errors[0]
    if timed_out:
        raise subprocess.TimeoutExpired(list(argv), timeout)
    return {
        "returncode": process.returncode,
        "stdout": bytes(buffers["stdout"]),
        "stderr": bytes(buffers["stderr"]),
        "stdout_overflow": stdout_overflow.is_set(),
        "stderr_overflow": stderr_overflow.is_set(),
        "stderr_watch_matched": stderr_watch_matched.is_set(),
        "streams_complete": not any(thread.is_alive() for thread in threads),
    }


def run_bzrk(args, timeout=bm_config.DEFAULT_TIMEOUT):
    """Run the bzrk CLI with the given argument list. Returns (text, is_error)."""
    if bm_config._RESOLVED_BZRK_BIN is None:
        return (
            f"error: '{bm_config._BZRK_BIN_CONFIG}' not found on PATH. Install the Berserk CLI or set BZRK_BIN to its full path."
        ), True
    args = list(args)
    # `bzrk search` auto-detects "agent mode" from the calling environment
    # (Claude Code / Codex set env vars a spawned child process inherits
    # unmodified) and switches to printing progressive `# Increment N`
    # snapshots instead of one final result -- confirmed by direct repro:
    # identical invocation, identical piped (non-TTY) stdout, produces a
    # clean single table without Claude Code's env vars present and a
    # streamed multi-snapshot dump with them present. Since berserk-mcp is
    # primarily deployed as an MCP server launched BY Claude Code, every
    # bzrk subprocess it spawns inherits that same detected context by
    # default. Force --no-stream unconditionally so parsing always sees a
    # single deterministic snapshot, and so a byte-cap or timeout can never
    # silently return an early partial increment as if it were complete.
    if "search" in args and "--no-stream" not in args:
        args = args + ["--no-stream"]
    with contextlib.ExitStack() as stack:
        # Every bzrk launch holds a query slot, so BERSERK_MCP_MAX_CONCURRENT_QUERIES
        # bounds all queries (schema, schema refresh and doctor queries call this
        # directly). Only --version is exempt: failing closed means a future query
        # subcommand is limited too. A caller that already holds a slot
        # (bzrk_search, the diagnostics path) does not take a second one.
        needs_slot = "--version" not in args and not bm_config._query_slot_held()
        if needs_slot and not stack.enter_context(bm_config._query_semaphore_slot(timeout)):
            return bm_config.QUERY_QUEUE_FULL_MESSAGE, True
        return _run_bzrk_launch(args, timeout)


def _run_bzrk_launch(args, timeout):
    """Launch one bzrk process for run_bzrk and classify its output."""
    try:
        result = _run_argv_bounded([bm_config._RESOLVED_BZRK_BIN] + args, timeout)
        out = result["stdout"].decode("utf-8", errors="replace").strip()
        err = result["stderr"].decode("utf-8", errors="replace").strip()
        if result.get("stderr_watch_matched") or (err and _AUTH_FAILURE_RE.search(err)):
            return bm_config.AUTH_FAILURE_MESSAGE, True
        if not result.get("streams_complete", True):
            # A reader outlived the child (e.g. a grandchild kept the pipe
            # open), so stderr was not fully scanned: fail closed.
            return "bzrk output could not be read completely; retry the query.", True
        if result["stdout_overflow"]:
            return (
                f"bzrk result exceeded BERSERK_MCP_MAX_RESULT_BYTES="
                f"{bm_config.MAX_BZRK_RESULT_BYTES}; narrow the time window, project fewer "
                "columns, or add a smaller take/top/tail bound."
            ), True
        if result["returncode"] != 0:
            diagnostic = (out + "\n" + err).strip() or f"bzrk exited {result['returncode']}"
            if len(diagnostic) > MAX_BZRK_DIAGNOSTIC_CHARS or result.get("stderr_overflow"):
                diagnostic = diagnostic[:MAX_BZRK_DIAGNOSTIC_CHARS] + "\n...[truncated]"
            return diagnostic, True
        return (out or "(no rows)"), False
    except FileNotFoundError:
        return (
            f"error: '{bm_config._BZRK_BIN_CONFIG}' not found on PATH. Install the Berserk CLI or set BZRK_BIN to its full path."
        ), True
    except subprocess.TimeoutExpired:
        return f"bzrk timed out after {timeout}s", True
    except Exception as e:  # pragma: no cover - defensive
        return ("error running bzrk: " + str(e)), True


def count_result_is_zero(text):
    """True if a `summarize n=count()`-style single-row result reports zero.

    `summarize count()` always emits one row even when nothing matches (n=0),
    so it never hits run_bzrk's "(no rows)" empty-stdout sentinel. Read the
    last whitespace-separated token of the last non-empty line — the count —
    regardless of whether bzrk renders it as a table, CSV, or plain value.
    """
    if not text or text.strip() == "(no rows)":
        return True
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return True
    tokens = lines[-1].split()
    if tokens and tokens[-1].lstrip("-").isdigit():
        return int(tokens[-1]) == 0
    return False


# Accepts "now" or "<n> <unit> [ago]" — e.g. "15m ago", "2 hours ago", "1d".
_SINCE_RE = re.compile(
    r"^(now|\d+\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days|w|wk|week|weeks)(\s+ago)?)$",
    re.IGNORECASE,
)


def valid_since(s):
    """Lightweight validation of a time window. Not a security control (the value
    is passed as an argv element, never a shell string) — purely a better error."""
    return bool(_SINCE_RE.match(str(s).strip())) and len(str(s)) <= 32


# Small models reach for these natural-language forms even when the schema
# asks for the canonical grammar. Map them onto a form _SINCE_RE already
# accepts, rather than rejecting a well-intentioned answer on syntax alone.
_SINCE_QUALIFIER_RE = re.compile(r"^\s*(?:in\s+the\s+last|over\s+the\s+last|last|past)\s+", re.IGNORECASE)


_SINCE_UNIT_ONLY_RE = re.compile(
    r"^(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days|w|wk|week|weeks)\s*$",
    re.IGNORECASE,
)


_SINCE_WORD_MAP = {"yesterday": "1d ago"}


def _normalize_since(s):
    """Map common natural-language time windows onto the canonical grammar
    _SINCE_RE accepts, before validation runs. Returns the input unchanged
    when it is already canonical or not recognized — this only widens the
    accepted spelling, never what reaches bzrk (the normalized output must
    still pass valid_since before it is used)."""
    raw = str(s)
    stripped = raw.strip()

    mapped = _SINCE_WORD_MAP.get(stripped.lower())
    if mapped is not None:
        return mapped
    if valid_since(raw):
        return raw

    qualified = _SINCE_QUALIFIER_RE.sub("", stripped, count=1)
    if qualified == stripped:
        return raw
    qualified = re.sub(r"\s+", " ", qualified).strip()

    # A bare unit noun with no leading number ("past week") implies quantity 1.
    if _SINCE_UNIT_ONLY_RE.match(qualified):
        qualified = "1 " + qualified
    if not qualified.lower().endswith("ago"):
        qualified += " ago"

    return qualified if valid_since(qualified) else raw


def _normalize_since_arg(args):
    """Normalize args['since'] in place, once, at the request boundary.

    Several consumers check `since` before ever reaching bzrk_search --
    claude_loop_check and other analytics tools call valid_since() directly,
    search/validate_kql/run_saved/save_query validate it via
    _validate_user_kql, and the modern-mode expensive_query_guard preflight
    inspects the raw argument before handle_call even runs. Normalizing only
    inside bzrk_search left all of those paths rejecting forms it had
    already learned to accept, and let an unbounded natural-language window
    (e.g. 'last 100 hours') skip the preflight confirmation its canonical
    equivalent ('100 hours ago') would have triggered. This must run before
    any of those checks, on every path that reaches them -- call it at the
    top of handle_call() (covers all tool branches and direct/test callers)
    and again before the modern preflight check in dispatch() (covers the
    JSON-RPC request path, which evaluates preflight before handle_call is
    invoked). Idempotent: normalizing an already-canonical value is a no-op.
    """
    if isinstance(args, dict) and isinstance(args.get("since"), str):
        args["since"] = _normalize_since(args["since"])


_BZRK_TIMEOUT_TEXT_RE = re.compile(r"^bzrk timed out after ", re.IGNORECASE)


def bzrk_search(kql, since, extra=None):
    """Run a KQL search on the configured profile and time window. `extra` adds
    trailing CLI flags (e.g. ['--json']) without duplicating the guards."""
    query = str(kql)
    boundary_error = _kql_boundary.check(query, bm_config.TABLE)
    if boundary_error:
        return boundary_error, True
    since = _normalize_since(since)
    if not valid_since(since):
        return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
    timeout = None
    tool_name = None
    fleet_context = bm_config._get_fleet_context()
    if fleet_context is not None:
        timeout = bm_config._window_budget(
            fleet_context.get("budget"),
            since,
            fleet_context.get("budget_multiplier", 1.0),
        )
        tool_name = fleet_context.get("tool")
    effective_timeout = timeout if timeout is not None else bm_config.DEFAULT_TIMEOUT
    with contextlib.ExitStack() as stack:
        # A caller that already holds a slot does not take a second one, so a
        # nested search can never deadlock against the default of two slots.
        needs_slot = not bm_config._query_slot_held()
        if needs_slot and not stack.enter_context(bm_config._query_semaphore_slot(effective_timeout)):
            return bm_config.QUERY_QUEUE_FULL_MESSAGE, True
        if timeout is None:
            out, is_err = run_bzrk(["-P", bm_config.PROFILE, "search", query, "--since", since] + list(extra or []))
        else:
            out, is_err = run_bzrk(
                ["-P", bm_config.PROFILE, "search", query, "--since", since] + list(extra or []),
                timeout=timeout,
            )
    if is_err and _BZRK_TIMEOUT_TEXT_RE.match(str(out or "")) and tool_name:
        return (
            f"{tool_name} exceeded its {timeout:g}s query budget for window {since!r}. "
            "Retry with a narrower 'since' window, or raise "
            "BERSERK_MCP_TOOL_BUDGET_SECONDS / BERSERK_MCP_BUDGET_PER_HOUR_SECONDS "
            "if this cluster is legitimately slower.",
            True,
        )
    # This is bzrk_search itself, the low-level fetch wrapper every dispatch
    # branch calls -- fencing belongs at the dispatch layer (the caller
    # decides whether/how to fence based on its own output shape), not
    # here; some non-dispatch callers (e.g. schema-fetching in
    # _schema_fetcher) legitimately need the raw value.
    return out, is_err  # nosemgrep: unfenced-bzrk-output-reaches-return


# bzrk builds that don't support --json reject it with an argument-parse
# error; detect that so we can transparently fall back to the default table
# output. Both known clap phrasings put the literal word "argument"
# immediately next to the (usually quoted) flag it's rejecting -- "unexpected
# argument '--json' found" / "Found argument '--json' which wasn't
# expected" -- so anchoring on "argument '--json'" is narrower and more
# reliable than matching on rejection-word vocabulary (a prior version
# matched words like "invalid" appearing anywhere near "--json", which also
# matched unrelated runtime errors merely mentioning the flag in passing).
# "argument '--json'" alone can still coincidentally appear in an unrelated
# message, so this additionally requires clap's own usage/help trailer,
# which every real clap argument-parse error appends and a genuinely
# unrelated backend/serialization error won't happen to also produce.
_JSON_UNSUPPORTED_RE = re.compile(r"(?i)argument\s*['\"]?--json['\"]?(?=.*?(usage:|--help))", re.DOTALL)


def bzrk_search_json(kql, since):
    """bzrk_search variant that requests --json for robust programmatic parsing
    (the analytics/secret modules parse rows in Python; aligned table output can
    truncate or ambiguously split wide `body` columns). Falls back to the
    default table output only when this bzrk build rejects the --json flag, so
    there is no regression on builds that lack it."""
    out, is_err = bzrk_search(kql, since, extra=["--json"])
    if is_err and _JSON_UNSUPPORTED_RE.search(out or ""):
        return bzrk_search(kql, since)
    # Same as bzrk_search above: low-level wrapper, fencing belongs at dispatch.
    return out, is_err  # nosemgrep: unfenced-bzrk-output-reaches-return


def do_schema():
    out1, e1 = run_bzrk(["-P", bm_config.PROFILE, "search", ".show tables"])
    out2, e2 = run_bzrk(["-P", bm_config.PROFILE, "search", f"{bm_queries.T} | getschema", "--since", "1h ago"])
    text = f"== tables ==\n{bm_fencing._fence_untrusted(out1)}\n== columns ==\n{bm_fencing._fence_untrusted(out2)}"
    return text, (e1 or e2)


def _schema_fetcher():
    """Fetch raw schema material for schema_registry.get_schema_snapshot().

    Contract (schema_registry.py): the fetcher must raise on failure so the
    caller's except-Exception falls back to a stale cache or reports
    "unavailable" -- it must never return error text as if it were real
    schema data, which would get normalized and cached as source_status
    "fresh". Any of the four run_bzrk calls failing (auth error, timeout,
    connection refused, etc.) fails the whole fetch, matching do_schema()'s
    existing any-fails semantics just above -- a partial result would still
    mean feeding one call's error text into normalize_snapshot as if it
    were real tables/columns/fields/sample data.
    """
    queries = (
        ("tables", [".show tables"]),
        ("getschema", [f"{bm_queries.T} | getschema", "--since", "1h ago"]),
        ("fieldstats", [bm_queries.q_discover_fieldstats(None), "--since", "1h ago"]),
        ("sample", [bm_queries.q_discover_sample(None), "--since", "1h ago"]),
    )
    results = {}
    for name, query in queries:
        out, is_err = run_bzrk(["-P", bm_config.PROFILE, "search", *query])
        if is_err:
            # Stop at the first failure: the caller holds schema_registry's
            # lock, and each further query could wait for a query slot.
            raise RuntimeError(f"schema fetch failed for: {name}")
        results[name] = out
    out_tables, out_schema = results["tables"], results["getschema"]
    out_fields, out_sample = results["fieldstats"], results["sample"]
    return {
        "tables": out_tables,
        "getschema": out_schema,
        "fieldstats": out_fields,
        "sample": out_sample,
        "supported_idioms": [
            "tail",
            "take",
            "top",
            "summarize",
            "make-series",
            "fieldstats",
            "series_decompose_anomalies",
            "series_fit_line",
            "similarto",
        ],
    }


def _schema_snapshot(force=False, allow_refresh=True):
    return schema_registry.get_schema_snapshot(
        force=force,
        table=bm_config.TABLE,
        config_dir=Path(bm_config.LEARNED_PATH).parent,
        fetcher=_schema_fetcher if allow_refresh else None,
    )


def _validation_schema(use_schema=True, allow_refresh=True):
    if not use_schema:
        return None, None, {"schema_status": "disabled"}
    try:
        snapshot = _schema_snapshot(force=False, allow_refresh=allow_refresh)
        fields = schema_registry.schema_fields(snapshot)
        info = {
            "schema_hash": snapshot.get("schema_hash"),
            "schema_status": snapshot.get("source_status", "unavailable"),
            "table": snapshot.get("table", bm_config.TABLE),
        }
        return snapshot, fields, info
    except Exception as e:
        bm_config.log(f"schema validation unavailable: {type(e).__name__}: {e}")
        return None, None, {"schema_status": "unavailable"}


def _validate_user_kql(kql, since, *, use_schema=True, allow_refresh_schema=True):
    base_report = kql_validation.validate_kql_static(
        str(kql or ""),
        table=bm_config.TABLE,
        since=str(since or ""),
        schema_fields=None,
        max_chars=bm_config.KQL_MAX_CHARS,
        max_rows=bm_config.KQL_MAX_ROWS,
        schema_info={"schema_status": "not_checked"},
    )
    if any(f.get("severity") == "error" for f in base_report.get("findings", [])) or not use_schema:
        return base_report
    snapshot, fields, info = _validation_schema(use_schema=use_schema, allow_refresh=allow_refresh_schema)
    report = kql_validation.validate_kql_static(
        str(kql or ""),
        table=bm_config.TABLE,
        since=str(since or ""),
        schema_fields=fields,
        max_chars=bm_config.KQL_MAX_CHARS,
        max_rows=bm_config.KQL_MAX_ROWS,
        schema_info=info,
        suggest=(lambda field: schema_registry.suggest_field(field, snapshot)) if snapshot else None,
    )
    return report


def _blocking_validation(report, *, persistence=False):
    if any(f.get("severity") == "error" for f in report.get("findings", [])):
        return True
    if bm_config.KQL_VALIDATION_MODE == "strict" and report.get("risk") == "high":
        return True
    return bool(persistence and report.get("risk") == "high")


def _format_validation_rejection(report):
    finding = next((f for f in report.get("findings", []) if f.get("severity") == "error"), None)
    if finding is None:
        finding = (report.get("findings") or [{"code": "HIGH_RISK", "message": "high-risk query"}])[0]
    prefix = "invalid KQL: " if finding.get("code") == "WRONG_TABLE" else ""
    return (
        f"{prefix}KQL rejected ({finding.get('code')}): {finding.get('message')} Estimated risk: {report.get('risk')}."
    )


def _format_validation_warnings(report):
    warnings = [f for f in report.get("findings", []) if f.get("severity") != "error"]
    if not warnings:
        return ""
    return "KQL validation warnings (risk={}):\n".format(report.get("risk")) + "\n".join(
        f"- {f.get('code')}: {f.get('message')}" for f in warnings[:8]
    )


def _parser_static_validation(kql, since):
    return _validate_user_kql(kql, since, use_schema=True)


def _parser_schema_context():
    snapshot = _schema_snapshot(force=False)
    return (
        schema_registry.schema_context(snapshot, max_chars=12000),
        snapshot.get("schema_hash", ""),
        snapshot.get("source_status", "unavailable"),
    )

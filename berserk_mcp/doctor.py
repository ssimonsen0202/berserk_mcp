"""The --doctor and self_check preflight, and admin commands.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import httpconfig as bm_httpconfig
from berserk_mcp import learned as bm_learned
from berserk_mcp import runner as bm_runner
from pathlib import Path
import _http
import agent_analytics
import json
import os
import parser_factory
import sys
import threading


_DOCTOR_REACHABILITY_TIMEOUT = 5


_DOCTOR_PROBE_SEMAPHORE = threading.BoundedSemaphore(2)


def _with_wall_clock_timeout(fn, timeout):
    """Run fn() with a genuine wall-clock deadline. urllib's own timeout=
    only bounds individual socket connect/read operations, not total time
    -- a server that sends the next byte just inside that window on every
    read can keep a request open far longer than the advertised timeout
    implies (measured: 8.02s wall clock against a 5s per-operation
    timeout). Returns None if fn() hasn't finished within `timeout`
    seconds. A plain daemon thread, not concurrent.futures.ThreadPoolExecutor:
    the executor registers an atexit hook that joins pending work, which
    would make the whole process hang waiting for exactly the slow call
    this exists to stop waiting on. The tradeoff this accepts: Python
    cannot forcibly kill a thread, so a truly stuck call keeps running in
    the background after this returns -- harmless for a one-shot preflight
    check, since the daemon thread cannot block process exit either."""
    # A stuck probe keeps its thread (and socket) after this returns, so bound
    # how many can exist: repeated self_check calls against a hung endpoint
    # would otherwise pile up threads. A slot is released only when the probe
    # itself finishes; with none free within `timeout`, report a timeout.
    if not _DOCTOR_PROBE_SEMAPHORE.acquire(timeout=timeout):
        return None
    box = {}

    def runner():
        try:
            box["result"] = fn()
        finally:
            _DOCTOR_PROBE_SEMAPHORE.release()

    t = threading.Thread(target=runner, daemon=True)
    try:
        t.start()
    except BaseException:
        _DOCTOR_PROBE_SEMAPHORE.release()
        raise
    t.join(timeout)
    if t.is_alive():
        return None
    return box.get("result")


def _doctor_result(name, status, detail, remediation=None, required=True):
    result = {"name": name, "status": status, "detail": detail, "required": required}
    if remediation:
        result["remediation"] = remediation
    return result


def _doctor_check_bzrk_resolvable(bzrk_bin_config=None, **resolve_kwargs):
    config = bm_config._BZRK_BIN_CONFIG if bzrk_bin_config is None else bzrk_bin_config
    try:
        resolved = bm_config._resolve_bzrk_binary(config, **resolve_kwargs)
    except ValueError as exc:
        return _doctor_result(
            "bzrk_resolvable",
            "fail",
            f"invalid BZRK_BIN: {exc}",
            remediation="set BZRK_BIN to an absolute path or a bare executable name",
        )
    if resolved is None:
        return _doctor_result(
            "bzrk_resolvable",
            "fail",
            f"{config!r} not found on PATH",
            remediation="install the Berserk CLI, or set BZRK_BIN to its absolute path",
        )
    return _doctor_result("bzrk_resolvable", "pass", f"resolved to {resolved}")


def _doctor_check_bzrk_version():
    out, err = bm_runner.run_bzrk(["--version"])
    if err:
        return _doctor_result(
            "bzrk_version",
            "fail",
            str(out)[:200],
            remediation="confirm `bzrk --version` runs from this environment",
        )
    # An exit-zero process that isn't actually bzrk (e.g. BZRK_BIN pointed
    # at /usr/bin/true, or run_bzrk's own synthetic "(no rows)") must not
    # satisfy this check just because nothing errored.
    text = str(out).strip()
    if not text.lower().startswith("bzrk"):
        return _doctor_result(
            "bzrk_version",
            "fail",
            f"output does not look like a bzrk version string: {text[:200]!r}",
            remediation="confirm BZRK_BIN actually points at the Berserk CLI",
        )
    return _doctor_result("bzrk_version", "pass", text)


def _doctor_check_auth():
    out, err = bm_runner.run_bzrk(
        ["-P", bm_config.PROFILE, "search", f"{bm_config.TABLE} | take 1", "--since", "15m ago"]
    )
    if err and str(out) == bm_config.AUTH_FAILURE_MESSAGE:
        return _doctor_result(
            "auth",
            "fail",
            "bzrk authentication failed",
            remediation="run `bzrk login` under profile " + repr(bm_config.PROFILE),
        )
    if err:
        # A non-auth error here is table_reachable's concern, not auth's --
        # don't double-report the same failure under two check names.
        return _doctor_result("auth", "skip", f"could not verify independently of query result: {out}"[:200])
    return _doctor_result("auth", "pass", f"authenticated under profile {bm_config.PROFILE!r}")


def _doctor_check_table_reachable():
    out, err = bm_runner.run_bzrk(
        ["-P", bm_config.PROFILE, "search", f"{bm_config.TABLE} | take 1", "--since", "15m ago"]
    )
    if err:
        return _doctor_result(
            "table_reachable",
            "fail",
            str(out)[:200],
            remediation=f"confirm BERSERK_TABLE={bm_config.TABLE!r} exists and profile {bm_config.PROFILE!r} can query it",
        )
    return _doctor_result(
        "table_reachable", "pass", f"{bm_config.TABLE!r} reachable under profile {bm_config.PROFILE!r}"
    )


def _doctor_check_recent_rows():
    # --stats' rows_returned counts response rows (always 1 for `| count`'s
    # single summary row), not the count value itself -- that value only
    # comes back in the row body, so this needs --json and the real
    # Tables/schema/rows shape (confirmed live), same parser used elsewhere
    # in this file for the same reason.
    out, err = bm_runner.run_bzrk(
        ["-P", bm_config.PROFILE, "search", f"{bm_config.TABLE} | count", "--since", "1h ago", "--json"]
    )
    if err:
        return _doctor_result(
            "recent_rows",
            "fail",
            str(out)[:200],
            remediation=f"confirm BERSERK_TABLE={bm_config.TABLE!r} is actively ingesting",
        )
    count = None
    try:
        records = agent_analytics._json_records(json.loads(out))
    except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
        records = None
    if records:
        row = records[0]
        if isinstance(row, dict):
            count = row.get("Count", row.get("count"))
    # This is a required check: a query that "succeeds" without yielding a
    # real count is not evidence the table is reachable and ingesting --
    # same failure mode as bzrk_version accepting any exit-zero output.
    if count is None:
        return _doctor_result(
            "recent_rows",
            "fail",
            f"query succeeded but no usable Count in the response: {str(out)[:150]!r}",
            remediation=f"confirm BERSERK_TABLE={bm_config.TABLE!r} and the bzrk build return the "
            "expected --json shape for `| count`",
        )
    return _doctor_result("recent_rows", "pass", f"{count} row(s) in the last 1h")


def _doctor_check_primers_dir():
    if bm_config.ACTIVE_ROLE not in bm_config._ROLE_PREFIX:
        return _doctor_result(
            "primers_dir",
            "skip",
            f"role {bm_config.ACTIVE_ROLE!r} has no associated primer",
            required=False,
        )
    env_dir = os.environ.get("BERSERK_MCP_PRIMERS_DIR", "")
    if env_dir:
        try:
            configured_dir = bm_config._validate_store_path(env_dir, "BERSERK_MCP_PRIMERS_DIR")
        except bm_config.StorePathError as exc:
            return _doctor_result(
                "primers_dir",
                "fail",
                f"invalid BERSERK_MCP_PRIMERS_DIR: {exc}",
                remediation="set BERSERK_MCP_PRIMERS_DIR to an absolute, existing directory",
            )
        primer_path = configured_dir / f"{bm_config.ACTIVE_ROLE}.md"
        if not primer_path.is_file():
            return _doctor_result(
                "primers_dir",
                "fail",
                f"{primer_path} not found",
                remediation=f"add {bm_config.ACTIVE_ROLE}.md under BERSERK_MCP_PRIMERS_DIR, or unset it to use the built-in primer",
            )
        return _doctor_result("primers_dir", "pass", f"{primer_path} readable")
    search_dirs = [
        bm_config.REPO_ROOT / "primers",
        Path(sys.prefix) / "share" / "berserk-mcp" / "primers",
    ]
    for primer_dir in search_dirs:
        if (primer_dir / f"{bm_config.ACTIVE_ROLE}.md").is_file():
            return _doctor_result("primers_dir", "pass", f"built-in primer found under {primer_dir}")
    # No BERSERK_MCP_PRIMERS_DIR override, so a missing built-in primer
    # degrades gracefully at runtime (empty primer text, not fatal) --
    # matches _load_primer's own tolerant behavior for this case.
    return _doctor_result(
        "primers_dir",
        "skip",
        f"no built-in primer for role {bm_config.ACTIVE_ROLE!r} (non-fatal)",
        required=False,
    )


def _doctor_check_tool_tier():
    """FR-5: --doctor/self_check exist to answer "is this wired the way I
    think?" -- a hidden tool is exactly that class of surprise, so the
    resolved tier is reported here too, not just at startup log time."""
    if bm_config.ACTIVE_TIER_RESOLVED == bm_config.TIER_SMALL:
        detail = (
            f"tier=small (role={bm_config.ACTIVE_ROLE}): {len(bm_config._DEEP_TIER_TOOLS)} tools hidden. "
            "Set BERSERK_MCP_TIER=deep to restore them."
        )
    else:
        detail = f"tier=deep (role={bm_config.ACTIVE_ROLE}): no tools hidden by tier."
    return _doctor_result("tool_tier", "pass", detail, required=False)


def _doctor_check_learned_store_writable():
    if bm_config.LEARNED_PATH.is_dir():
        return _doctor_result(
            "learned_store_writable",
            "fail",
            f"{bm_config.LEARNED_PATH} already exists as a directory",
            remediation="BERSERK_MCP_LEARNED_PATH must name a file, not a "
            "directory -- the atomic save would fail with IsADirectoryError",
        )
    parent = bm_config.LEARNED_PATH.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _doctor_result(
            "learned_store_writable",
            "fail",
            f"cannot create {parent}: {exc}",
            remediation="confirm the parent of BERSERK_MCP_LEARNED_PATH is writable",
        )
    if not os.access(parent, os.W_OK):
        return _doctor_result(
            "learned_store_writable",
            "fail",
            f"{parent} is not writable",
            remediation="confirm the parent of BERSERK_MCP_LEARNED_PATH is writable",
        )
    return _doctor_result("learned_store_writable", "pass", f"{parent} writable")


def _doctor_check_http_config():
    if not bm_config.HTTP_ENABLE:
        return _doctor_result(
            "http_config",
            "skip",
            "BERSERK_MCP_HTTP_ENABLE is not set",
            required=False,
        )
    try:
        # _build_http_config's keyword defaults are bound once at import
        # time, not at call time -- pass the current module globals
        # explicitly so this check reflects the live environment, not
        # whatever the values were when the module was first imported.
        config = bm_httpconfig._build_http_config(
            enable=bm_config.HTTP_ENABLE,
            bind=bm_config.HTTP_BIND,
            allow_remote=bm_config.HTTP_ALLOW_REMOTE,
            auth_token=bm_config.HTTP_AUTH_TOKEN,
            allowed_hosts=bm_config.HTTP_ALLOWED_HOSTS,
            allow_cidrs=bm_config.HTTP_ALLOW_CIDRS,
            max_request_bytes=bm_config.HTTP_MAX_REQUEST_BYTES,
            max_concurrent_requests=bm_config.HTTP_MAX_CONCURRENT_REQUESTS,
            use_forwarded_for=bm_config.HTTP_USE_FORWARDED_FOR,
            trusted_proxy_cidrs=bm_config.HTTP_TRUSTED_PROXY_CIDRS,
        )
    except bm_httpconfig.HttpConfigError as exc:
        return _doctor_result(
            "http_config",
            "fail",
            str(exc),
            remediation="fix the HTTP env var named in the error above",
        )
    return _doctor_result("http_config", "pass", f"coherent, binds {config['host']}:{config['port']}")


def _doctor_check_llm_reachability():
    # parser_factory._hermes_url() always resolves to SOME URL -- it falls
    # back to a hardcoded localhost default when nothing is explicitly
    # configured, and generation features attempt that default outright.
    # So "unconfigured" never means "nothing will be attempted"; skipping
    # in that case would report exit_code=0 while generation would still
    # fail hitting an unreachable default. Always probe the real effective
    # URL, staying optional (required=False) so an operator who doesn't use
    # generation features still isn't pushed to "broken" by it.
    configured = bool(os.environ.get("BERSERK_LLM_HERMES_URL")) or bool(parser_factory._llm_config().get("hermes_url"))
    url = parser_factory._hermes_url()
    url_note = f" (using the unconfigured default {url!r})" if not configured else ""
    models_url = parser_factory.hermes_models_url(url)
    if not models_url:
        return _doctor_result(
            "llm_hermes_reachability",
            "fail",
            f"cannot derive a /models endpoint from {url!r}",
            remediation="confirm BERSERK_LLM_HERMES_URL points at a chat/completions endpoint",
            required=False,
        )
    key = os.environ.get("HERMES_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    outcome = _with_wall_clock_timeout(
        lambda: _http.http_get_json(models_url, headers, timeout=_DOCTOR_REACHABILITY_TIMEOUT),
        _DOCTOR_REACHABILITY_TIMEOUT,
    )
    if outcome is None:
        return _doctor_result(
            "llm_hermes_reachability",
            "fail",
            f"timed out after {_DOCTOR_REACHABILITY_TIMEOUT}s probing {models_url}{url_note}",
            remediation="Hermes is not responding in time; confirm it's running and reachable",
            required=False,
        )
    _out, err = outcome
    if err:
        return _doctor_result(
            "llm_hermes_reachability",
            "fail",
            f"unreachable at {models_url}{url_note}: {err}",
            remediation="confirm Hermes is running and BERSERK_LLM_HERMES_URL is correct"
            if configured
            else "set BERSERK_LLM_HERMES_URL (or run `berserk-mcp --set-hermes-url`) "
            "if you use generation features, or ignore this if you don't",
            required=False,
        )
    return _doctor_result(
        "llm_hermes_reachability",
        "pass",
        f"reachable at {models_url}{url_note}",
        required=False,
    )


def _doctor_check_canonloom_reachability():
    server_url = os.environ.get("CANONLOOM_SERVER_URL", "").rstrip("/")
    if not server_url:
        return _doctor_result(
            "canonloom_reachability",
            "skip",
            "CANONLOOM_SERVER_URL not set; CanonLoom tools are optional",
            required=False,
        )
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("CANONLOOM_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    outcome = _with_wall_clock_timeout(
        lambda: _http.http_get_json(
            server_url + "/artifacts", headers, timeout=_DOCTOR_REACHABILITY_TIMEOUT, allow_plaintext_remote=False
        ),
        _DOCTOR_REACHABILITY_TIMEOUT,
    )
    if outcome is None:
        return _doctor_result(
            "canonloom_reachability",
            "fail",
            f"timed out after {_DOCTOR_REACHABILITY_TIMEOUT}s probing {server_url}",
            remediation="CanonLoom is not responding in time; confirm it's running and reachable",
            required=False,
        )
    _out, err = outcome
    if err:
        return _doctor_result(
            "canonloom_reachability",
            "fail",
            f"unreachable at {server_url}: {err}",
            remediation="confirm canonloom-server is running at CANONLOOM_SERVER_URL",
            required=False,
        )
    return _doctor_result("canonloom_reachability", "pass", f"reachable at {server_url}", required=False)


def _doctor_check_egress_policy():
    """Report the effective outbound destination policy (_http egress policy)."""
    try:
        active = _http.egress_policy_active()
        hosts = sorted(_http.egress_allowed_hosts())
        networks = [str(n) for n in _http.egress_allowed_networks()]
    except _http.UrlPolicyError as exc:
        return _doctor_result(
            "egress_policy",
            "fail",
            str(exc),
            remediation="fix BERSERK_EGRESS_ALLOWED_CIDRS; outbound calls are refused until it parses",
            required=False,
        )
    if not active:
        return _doctor_result(
            "egress_policy",
            "pass",
            "inactive: integrations may reach any host (HTTPS, redirect and plaintext rules still apply)",
            required=False,
        )
    parts = ["active"]
    if _http.local_only_enabled():
        cloud = [p for p in parser_factory.ladder() if p in parser_factory._CLOUD_PROVIDERS]
        parts.append("BERSERK_LOCAL_ONLY" + (f" (refusing {', '.join(cloud)} in the ladder)" if cloud else ""))
    parts.append("loopback")
    parts.append("hosts: " + (", ".join(hosts) or "none"))
    parts.append("networks: " + (", ".join(networks) or "none"))
    return _doctor_result("egress_policy", "pass", "; ".join(parts), required=False)


_DOCTOR_CHECK_FUNCS = (
    ("bzrk_resolvable", _doctor_check_bzrk_resolvable),
    ("bzrk_version", _doctor_check_bzrk_version),
    ("auth", _doctor_check_auth),
    ("table_reachable", _doctor_check_table_reachable),
    ("recent_rows", _doctor_check_recent_rows),
    ("primers_dir", _doctor_check_primers_dir),
    ("tool_tier", _doctor_check_tool_tier),
    ("learned_store_writable", _doctor_check_learned_store_writable),
    ("http_config", _doctor_check_http_config),
    ("egress_policy", _doctor_check_egress_policy),
    ("llm_hermes_reachability", _doctor_check_llm_reachability),
    ("canonloom_reachability", _doctor_check_canonloom_reachability),
)


# table_reachable and recent_rows query the same profile as auth. If auth
# has already failed, re-running them would just fail the same way for the
# same reason -- reporting three separate "fail" rows for one root cause
# is misleading, not more informative. Skip them and point at auth instead.
_DOCTOR_AUTH_DEPENDENT_CHECKS = frozenset({"table_reachable", "recent_rows"})


def _run_doctor_checks():
    """Ordered preflight checks. Each runs independently and is isolated
    from the others: one check failing does not skip the rest (except the
    deliberate auth-dependency short-circuit below), and one check raising
    an unexpected exception produces a failed result for just that check
    rather than losing the whole report -- a preflight tool must never
    itself crash."""
    results = []
    auth_failed = False
    for name, fn in _DOCTOR_CHECK_FUNCS:
        if name in _DOCTOR_AUTH_DEPENDENT_CHECKS and auth_failed:
            results.append(
                _doctor_result(
                    name,
                    "skip",
                    "skipped: auth already failed, so this check's own result "
                    "would be uninformative -- see the auth check above",
                )
            )
            continue
        try:
            result = fn()
        except Exception as exc:
            result = _doctor_result(
                name,
                "fail",
                f"check raised {type(exc).__name__}: {exc}"[:200],
                remediation="this looks like a bug in berserk-mcp's own "
                "doctor check, not your configuration; please report it",
            )
        results.append(result)
        if name == "auth":
            auth_failed = result["status"] == "fail"
    return results


def _doctor_exit_code(results):
    """0 pass / 1 degraded / 2 broken. A failed required check means the
    server cannot do its core job (broken); a failed optional check means
    an opt-in integration isn't working but the server still can
    (degraded); skips never lower the exit code."""
    if any(r["status"] == "fail" and r.get("required", True) for r in results):
        return 2
    if any(r["status"] == "fail" for r in results):
        return 1
    return 0


def _format_doctor_table(results):
    lines = [f"{'CHECK':<28} {'STATUS':<6} DETAIL"]
    for r in results:
        lines.append(f"{r['name']:<28} {r['status'].upper():<6} {r['detail']}")
        if r["status"] == "fail" and r.get("remediation"):
            lines.append(f"{'':<28} {'':<6} -> {r['remediation']}")
    return "\n".join(lines)


def run_doctor(json_output=False):
    """Preflight readiness check. Prints a pass/fail/skip table (or JSON
    with --json) and returns 0/1/2 -- usable as a fleet readiness probe or
    a container healthcheck, not just interactive output.

    Cannot help with every misconfiguration: an invalid BZRK_BIN (line
    ~132) or a BERSERK_MCP_PRIMERS_DIR pointed at a missing primer file
    (line ~430) both fail the whole process at import time, before main()
    ever parses --doctor. That's deliberate, predates this function, and
    isn't something a preflight check run from inside the same process can
    route around -- weakening either to let --doctor limp past would also
    weaken the fail-fast guarantee for every other code path that isn't
    --doctor. Those two cases exit with a direct, specific error message on
    stderr instead; --doctor covers everything that doesn't already fail
    that way (see _doctor_check_bzrk_resolvable and _doctor_check_primers_dir
    for the cases that don't hard-exit -- bzrk simply missing from PATH, or
    a missing built-in primer with no PRIMERS_DIR override)."""
    results = _run_doctor_checks()
    code = _doctor_exit_code(results)
    if json_output:
        print(json.dumps({"checks": results, "exit_code": code}, indent=2, sort_keys=True))
    else:
        print(_format_doctor_table(results))
    return code


def _attach_fingerprints(record, model):
    """Fingerprints are additive and best-effort: a fetch failure must not
    discard a real, already-scored canary result, so every failure here is
    caught and logged, never raised past this function.

    Imports fingerprint locally rather than relying on a caller having
    already imported it as a module global -- that reliance was itself a
    bug. `fingerprint` was never a module global anywhere in this file,
    only a name local to main()'s own --canary-run branch, so every real
    call to this function raised NameError, silently (caught and logged
    below, never propagated) -- found by an independent Codex review
    (2026-09-02) after this shipped through 6 clean task reviews and a
    clean final whole-branch review, none of which had actually invoked
    this function; confirmed live via
    `PYTHONPATH=. python3 -c "import berserk_mcp as b; r={}; b._attach_fingerprints(r,'x'); print(r)"`.
    """
    sys.path.insert(0, str(bm_config.REPO_ROOT / "evals"))
    import fingerprint
    import parser_factory

    try:
        url = parser_factory._hermes_url()
        key = os.environ.get("HERMES_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        completions = []
        for prompt in fingerprint.FINGERPRINT_PROMPTS:
            out, err = parser_factory._http_post_json(
                url,
                headers,
                {
                    "model": model,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if err:
                raise RuntimeError(err)
            completions.append(out["choices"][0]["message"]["content"])
        record["eval.behavioral_fingerprint"] = fingerprint.behavioral_fingerprint(completions)

        # parser_factory has _http_post_json but no GET-JSON helper --
        # confirmed by reading the module while writing this plan.
        # Use Task 3's fetch_models() for the metadata half instead.
        payload, err = fingerprint.fetch_models(url, key)
        if err:
            raise RuntimeError(err)
        meta_fp = fingerprint.metadata_fingerprint(payload, model)
        # metadata_fingerprint() returning None (fetch succeeded, no error)
        # means the model is no longer listed by the provider -- a real
        # signal, not nothing. Record it distinctly rather than silently
        # omitting the attribute, so a model vanishing from the catalog
        # shows up as a fingerprint change (found by Codex review,
        # 2026-09-02).
        record["eval.provider_metadata_fingerprint"] = meta_fp or "absent"
    except Exception as exc:  # noqa: BLE001
        print(f"fingerprint skipped for {model}: {exc}", file=sys.stderr)


def _terminal_safe(value):
    """Show LLM-authored text on an operator's terminal without letting it
    emit control sequences (ANSI escapes could hide or forge the receipt)."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in str(value))


def _cli_approve_generated(name):
    """--approve-generated: approve, print exactly what was approved, return the exit code."""
    entry, error = bm_learned.approve_generated_query(name)
    if error:
        print(f"not approved: {_terminal_safe(error)}", file=sys.stderr)
        return 2
    generated_by = entry.get("generated_by") or {}
    safe = _terminal_safe
    print(f"approved {safe(repr(entry['name']))} at {safe(entry['approved_at'])}")
    print(f"  description: {safe(entry.get('description', ''))}")
    print(f"  since:       {safe(entry.get('since', ''))}")
    print(
        f"  generated:   {safe(generated_by.get('provider', '?'))}/{safe(generated_by.get('model', '?'))} "
        f"@ {safe(generated_by.get('ts', '?'))}"
    )
    print(f"  kql:         {safe(entry.get('kql', ''))}")
    return 0


def _run_admin_command(ns):
    """One-shot operator commands that exit without starting a server.
    Returns an exit code, or None when none was requested."""
    if ns.approve_generated:
        return _cli_approve_generated(ns.approve_generated)
    if ns.doctor:
        return run_doctor(json_output=ns.json)
    return None

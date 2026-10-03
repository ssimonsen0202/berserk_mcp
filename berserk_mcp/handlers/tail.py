"""Handlers for the tail and CanonLoom tools.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import runner as bm_runner
from berserk_mcp import tools as bm_tools
import ai_finops
import ingestion_advisor
import secret_scan


def _handle_tail_core(name, arguments):
    """Recommendation decisions, secret scan, ingestion advisor. Returns (text, is_error) or None."""
    if name == "claude_record_recommendation_decision":
        return ai_finops.record_recommendation_decision(
            arguments.get("recommendation_id"),
            arguments.get("decision"),
            arguments.get("owner"),
            arguments.get("rationale"),
        )
    if name == "scan_secrets":
        since = arguments.get("since") or "1h ago"
        if not bm_runner.valid_since(since):
            return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
        include_entropy = arguments.get("include_entropy", False)
        if not isinstance(include_entropy, bool):
            return "'include_entropy' must be a boolean", True
        include_pii = arguments.get("include_pii") or []
        if not isinstance(include_pii, list) or any(item not in secret_scan.ALL_PII_TYPES for item in include_pii):
            return ("'include_pii' must be a list containing only: email, ipv4, ipv6, credit_card"), True
        return secret_scan.scan_secrets(
            since,
            include_entropy=include_entropy,
            pii_types=include_pii,
        )
    if name == "suggest_ingestion":
        role_or_usecase = arguments.get("role_or_usecase")
        if not isinstance(role_or_usecase, str) or not role_or_usecase.strip():
            return "missing required 'role_or_usecase'", True
        check_gap = arguments.get("check_gap", False)
        if not isinstance(check_gap, bool):
            return "'check_gap' must be a boolean", True
        since = arguments.get("since") or "24h ago"
        if not bm_runner.valid_since(since):
            return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
        return ingestion_advisor.suggest_ingestion(
            role_or_usecase,
            check_gap=check_gap,
            since=since,
        )
    return None


def _handle_canonloom(name, arguments):
    """CanonLoom knowledge-pipeline tools. Returns (text, is_error) or None."""
    if name == "canonloom_run_pipeline":
        url = str(arguments.get("url", "")).strip()
        if not url:
            return "canonloom_run_pipeline requires 'url'", True
        body = {"url": url}
        if "stop_after" in arguments:
            body["stop_after"] = arguments["stop_after"]
        # auto_promote is an authorization gate that defaults OFF: a
        # malformed/non-boolean value (e.g. the string "false", which is
        # truthy in Python) must fail closed and NOT authorize an
        # unattended promotion (SEC-05, Codex security review; same class
        # of bug save_query's overwrite= guards against a few hundred
        # lines up).
        if "auto_promote" in arguments:
            body["auto_promote"] = arguments["auto_promote"] is True
        # record_telemetry is an audit control that defaults ON: a
        # malformed/non-boolean value must fail OPEN (keep recording), not
        # silently disable the audit trail. This is the opposite polarity
        # from auto_promote above -- only a real, literal False turns it
        # off (Codex re-review finding on an earlier `is True` fix here,
        # which coerced any non-exact-True value, including a caller's
        # mistyped boolean, to False).
        if "record_telemetry" in arguments:
            body["record_telemetry"] = arguments["record_telemetry"] is not False
        return bm_tools._canonloom_call("/pipeline/run", "POST", body)
    if name == "canonloom_list_artifacts":
        if arguments.get("include_staging") is True:
            promoted, err = bm_tools._canonloom_call("/artifacts", "GET")
            staging, serr = bm_tools._canonloom_call("/artifacts/staging", "GET")
            if err:
                return promoted, True
            if serr:
                return staging, True
            import json as _json

            try:
                p = _json.loads(promoted) if isinstance(promoted, str) else promoted
                s = _json.loads(staging) if isinstance(staging, str) else staging
                combined = {"artifacts": p.get("artifacts", []) + s.get("artifacts", [])}
                return _json.dumps(combined), False
            except Exception as exc:
                return f"error merging artifact lists: {exc}", True
        return bm_tools._canonloom_call("/artifacts", "GET")
    if name == "canonloom_get_artifact":
        artifact_id = str(arguments.get("artifact_id", "")).strip()
        if not artifact_id:
            return "canonloom_get_artifact requires 'artifact_id'", True
        return bm_tools._canonloom_call(f"/artifacts/{artifact_id}", "GET")
    if name == "canonloom_freshness_report":
        body = {}
        if "half_life_days" in arguments:
            body["half_life_days"] = int(arguments["half_life_days"])
        if "min_age_days" in arguments:
            body["min_age_days"] = int(arguments["min_age_days"])
        return bm_tools._canonloom_call("/telemetry/freshness", "POST", body)
    if name == "canonloom_run_history":
        params = []
        if "status" in arguments:
            params.append(f"status={arguments['status']}")
        limit = int(arguments.get("limit", 20))
        params.append(f"limit={limit}")
        qs = "?" + "&".join(params) if params else ""
        return bm_tools._canonloom_call(f"/telemetry/runs{qs}", "GET")
    return None


def _handle_tail(name, arguments):
    """Tail tools: recommendation decisions, secret scan, ingestion advisor, CanonLoom. Returns (text, is_error) or None."""
    result = _handle_tail_core(name, arguments)
    if result is not None:
        return result
    return _handle_canonloom(name, arguments)

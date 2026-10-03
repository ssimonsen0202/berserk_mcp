"""JSON-RPC plumbing and the stdio and HTTP transports.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import httpconfig as bm_httpconfig
from berserk_mcp import learned as bm_learned
from berserk_mcp import runner as bm_runner
from berserk_mcp import tools as bm_tools
from berserk_mcp._version import __version__ as __version__
from berserk_mcp.handlers import dispatch as bm_dispatch
from contextlib import suppress
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import re
import secret_scan
import sys
import threading
import time
import uuid


# ---------- JSON-RPC plumbing ----------
# BUG-005 (2026-07-18 security review): three real defects fixed together
# here, since they're all about dispatch() trusting shapes it must not:
#   1. dispatch([]) (or any non-dict top-level value) raised an uncaught
#      AttributeError from req.get(...) -- confirmed live -- which propagated
#      out of main()'s loop with no handler and killed the whole server
#      process. A single malformed line from a connected stdio client was a
#      full process-level denial of service.
#   2. Every request branch (tools/call, initialize, tools/list, ping)
#      unconditionally returned a response dict, even when the incoming
#      message had no "id" -- i.e. was itself a notification. Only the
#      unknown-method fallback checked for that. Notifications are one-way
#      by JSON-RPC/MCP definition; a client sending e.g. a tools/call
#      notification got a response anyway.
#   3. initialize echoed back whatever protocolVersion the client sent,
#      instead of negotiating: this server implements exactly one version
#      (PROTOCOL_VERSION), so it must report that version regardless of
#      what the client claims to speak.
def _jsonrpc_error(code, message, id_=None):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _jsonrpc_result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _jsonrpc_unsupported_protocol(id_, requested):
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {
            "code": -32022,
            "message": "Unsupported protocol version",
            "data": {
                "supported": list(bm_config.SUPPORTED_PROTOCOL_VERSIONS),
                "requested": requested,
            },
        },
    }


def _valid_mcp_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _modern_mcp_enabled():
    return bool(bm_config.ENABLE_MCP_2026_07_28)


def _request_meta(params):
    """Return a validated modern-MCP metadata object, or None if malformed.

    MCP 2026-07-28 moves protocol information into per-request ``_meta``.
    Phase 1 only adds internal mode selection; actual modern methods are added
    in later phases.
    """
    meta = params.get("_meta") if isinstance(params, dict) else None
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        return None
    return meta


def _requested_protocol_version(params):
    meta = _request_meta(params)
    if meta is None:
        return None
    version = meta.get(bm_config.MCP_META_PROTOCOL_VERSION)
    if version is None:
        version = meta.get("protocolVersion")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _protocol_mode_for_request(method, params):
    """Select the internal MCP compatibility mode for a validated request.

    Default behavior stays legacy. Modern mode is selected only when the
    explicit feature flag is on and the request advertises the modern protocol
    through per-request metadata.
    """
    del method  # reserved for method-specific routing in Phase 2+
    if _modern_mcp_enabled() and _requested_protocol_version(params) == bm_config.MCP_PROTOCOL_MODERN:
        return bm_config.PROTOCOL_MODE_MODERN
    return bm_config.PROTOCOL_MODE_LEGACY


def _valid_modern_meta(params):
    meta = _request_meta(params)
    if meta is None:
        return False
    requested = _requested_protocol_version(params)
    caps = meta.get(bm_config.MCP_META_CLIENT_CAPABILITIES)
    client_info = meta.get(bm_config.MCP_META_CLIENT_INFO)
    return requested == bm_config.MCP_PROTOCOL_MODERN and isinstance(caps, dict) and isinstance(client_info, dict)


def _list_changed_supported():
    """True only when a save could plausibly reach the client: stdio can
    push notifications/tools/list_changed at any time (see _TRANSPORT),
    and there must be something for it to announce -- a saved__* projection
    disabled via SAVED_TOOL_PROJECTION_CAP=0 never changes tools/list at
    all, so advertising the capability would just be noise."""
    return _TRANSPORT == "stdio" and bm_learned.SAVED_TOOL_PROJECTION_CAP > 0


# 2026-07-28 delivers list-changed notifications only on a subscriptions/listen
# stream the client opened, and the server MUST NOT send a type the client did
# not request. A stdio process serves one client, so this is per-process state.
_LISTEN_FILTER_BOOL_KEYS = frozenset({"toolsListChanged", "promptsListChanged", "resourcesListChanged"})


_LISTEN_SUBSCRIPTIONS = {}


# Guards _LISTEN_SUBSCRIPTIONS and delivery, so nothing is sent after a cancel
# (task threads notify while the main loop handles listen/cancel).
_LISTEN_LOCK = threading.Lock()


_MAX_LISTEN_SUBSCRIPTIONS = 1024  # the SDK reference server's default


_MODERN_STDIO_CLIENT = False


def _note_request_mode(mode):
    global _MODERN_STDIO_CLIENT
    if mode == bm_config.PROTOCOL_MODE_MODERN and _TRANSPORT == "stdio":
        _MODERN_STDIO_CLIENT = True


def _valid_listen_filter(requested):
    # Unknown keys are ignored, not rejected: the SDK's SubscriptionFilter schema strips them.
    if not isinstance(requested, dict):
        return False
    if any(not isinstance(requested[k], bool) for k in _LISTEN_FILTER_BOOL_KEYS & set(requested)):
        return False
    uris = requested.get("resourceSubscriptions", [])
    return isinstance(uris, list) and all(isinstance(u, str) for u in uris)


def _dispatch_listen(params, id_, is_notification, mode):
    """subscriptions/listen: acknowledge on stdio, then hold the request open.

    The listen request gets no response while the subscription lives; the spec
    answers it only on graceful teardown. Tools are the only list this server
    changes, so only toolsListChanged is ever agreed to.
    """
    if is_notification:
        return None
    if mode != bm_config.PROTOCOL_MODE_MODERN or _TRANSPORT != "stdio":
        return _jsonrpc_error(-32601, "Method not found", id_)
    requested = params.get("notifications")
    if (
        set(params) - {"_meta", "notifications"}
        or not _valid_modern_meta(params)
        or not _valid_listen_filter(requested)
    ):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    agreed = (
        {"toolsListChanged": True} if requested.get("toolsListChanged") is True and _list_changed_supported() else {}
    )
    with _LISTEN_LOCK:
        if id_ in _LISTEN_SUBSCRIPTIONS:
            return _jsonrpc_error(-32602, "Invalid params", id_)
        if len(_LISTEN_SUBSCRIPTIONS) >= _MAX_LISTEN_SUBSCRIPTIONS:
            return _jsonrpc_error(-32603, "Subscription limit reached", id_)
        _LISTEN_SUBSCRIPTIONS[id_] = agreed
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {"_meta": {bm_config.MCP_META_SUBSCRIPTION_ID: id_}, "notifications": agreed},
            }
        )
    return None


def _notify_tools_list_changed():
    """Legacy clients get the unsolicited notification; a modern client gets it
    only on each listen stream that opted in to toolsListChanged."""
    if not _list_changed_supported():
        return
    if not _MODERN_STDIO_CLIENT:
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return
    with _LISTEN_LOCK:
        for sub_id, agreed in _LISTEN_SUBSCRIPTIONS.items():
            if agreed.get("toolsListChanged"):
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/tools/list_changed",
                        "params": {"_meta": {bm_config.MCP_META_SUBSCRIPTION_ID: sub_id}},
                    }
                )


def _notify_tools_list_changed_hook():
    # Looks the sender up at call time, so a test that replaces
    # _notify_tools_list_changed is still the one that runs.
    _notify_tools_list_changed()


bm_learned.set_tools_changed_notifier(_notify_tools_list_changed_hook)


def _discover_result():
    capabilities = {
        "tools": {"listChanged": _list_changed_supported()},
        "extensions": {
            "tasks": {
                "uri": bm_config.MCP_TASK_EXTENSION_URI,
                "methods": ["tasks/get", "tasks/cancel"],
                "createHint": "Set arguments.as_task=true on eligible long-running tools.",
            }
        },
    }
    return {
        "resultType": "complete",
        "supportedVersions": [bm_config.MCP_PROTOCOL_MODERN, bm_config.MCP_PROTOCOL_LEGACY],
        "capabilities": capabilities,
        "_meta": {
            bm_config.MCP_META_SERVER_INFO: bm_config.SERVER_INFO,
        },
        "instructions": bm_config.INSTRUCTIONS,
        # Role and environment can change tool visibility/instructions, so this
        # is cacheable only for the current caller/deployment context.
        "ttlMs": bm_config.MCP_PRIVATE_CACHE_TTL_MS,
        "cacheScope": "private",
    }


_TASKS = {}


_TASK_LOCK = threading.RLock()


def _task_now():
    return time.time()


def _task_public(record):
    return {
        "id": record["id"],
        "status": record["status"],
        "tool": record["tool"],
        "createdAt": record["created_at"],
        "updatedAt": record["updated_at"],
        "expiresAt": record["expires_at"],
    }


def _task_result(record):
    payload = {"resultType": "complete", "task": _task_public(record)}
    if record.get("result") is not None:
        payload["result"] = record["result"]
    if record.get("error"):
        payload["error"] = record["error"]
    return payload


def _task_prune_locked(now=None):
    now = _task_now() if now is None else now
    expired = [task_id for task_id, record in _TASKS.items() if record.get("expires_ts", 0) <= now]
    for task_id in expired:
        _TASKS.pop(task_id, None)
    if len(_TASKS) > bm_config.MCP_MAX_TASKS:
        removable = sorted(
            (
                (record.get("updated_ts", 0), task_id)
                for task_id, record in _TASKS.items()
                if record.get("status") in {"complete", "failed", "cancelled"}
            )
        )
        for _, task_id in removable[: len(_TASKS) - bm_config.MCP_MAX_TASKS]:
            _TASKS.pop(task_id, None)


def _launch_task_worker(target):
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    return worker


def _execute_task_tool(name, arguments, mode):
    text, is_err = bm_dispatch.handle_call(name, arguments)
    text = secret_scan.apply_output_filter(
        text,
        mode=bm_config.REDACT_MODE,
        include_entropy=bm_config.REDACT_ENTROPY,
        pii_types=bm_config.REDACT_PII_TYPES,
    )
    return _tool_call_result(name, text, is_err, mode)


def _run_task(task_id, name, arguments, mode):
    with _TASK_LOCK:
        record = _TASKS.get(task_id)
        if record is None or record.get("status") == "cancelled":
            return
        record["status"] = "running"
        record["updated_ts"] = _task_now()
        record["updated_at"] = bm_config.now_iso()
    try:
        result = _execute_task_tool(name, arguments, mode)
        status = "complete"
        error = ""
    except Exception as exc:  # pragma: no cover - defensive boundary
        result = None
        status = "failed"
        error = type(exc).__name__
    with _TASK_LOCK:
        record = _TASKS.get(task_id)
        if record is None or record.get("status") == "cancelled":
            return
        record["status"] = status
        record["result"] = result
        record["error"] = error
        record["updated_ts"] = _task_now()
        record["updated_at"] = bm_config.now_iso()


def _create_task(name, arguments, mode):
    now = _task_now()
    task_id = "task_" + uuid.uuid4().hex
    record = {
        "id": task_id,
        "status": "pending",
        "tool": name,
        "role": bm_config.ACTIVE_ROLE,
        "created_ts": now,
        "updated_ts": now,
        "expires_ts": now + bm_config.MCP_TASK_TTL_SECONDS,
        "created_at": bm_config.now_iso(),
        "updated_at": bm_config.now_iso(),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + bm_config.MCP_TASK_TTL_SECONDS)),
        "result": None,
        "error": "",
    }
    with _TASK_LOCK:
        _task_prune_locked(now)
        if len(_TASKS) >= bm_config.MCP_MAX_TASKS:
            return None
        _TASKS[task_id] = record
    _launch_task_worker(lambda: _run_task(task_id, name, dict(arguments), mode))
    return {"resultType": "task", "task": _task_public(record)}


def _task_lookup(task_id):
    with _TASK_LOCK:
        _task_prune_locked()
        record = _TASKS.get(task_id)
        if record is None or record.get("role") != bm_config.ACTIVE_ROLE:
            return None
        return dict(record)


def _client_supports_tasks(params):
    meta = _request_meta(params)
    if meta is None:
        return False
    caps = meta.get(bm_config.MCP_META_CLIENT_CAPABILITIES)
    if not isinstance(caps, dict):
        return False
    extensions = caps.get("extensions")
    return isinstance(caps.get("tasks"), dict) or (
        isinstance(extensions, dict) and ("tasks" in extensions or bm_config.MCP_TASK_EXTENSION_URI in extensions)
    )


def _task_id_from_params(params):
    task_id = params.get("taskId") or params.get("id")
    if not isinstance(task_id, str) or not re.fullmatch(r"task_[a-f0-9]{32}", task_id):
        return None
    return task_id


def _tool_list_result(mode):
    allt = [
        t for t in bm_tools.TOOLS + bm_tools.MGMT_TOOLS + bm_learned._saved_query_tools() if bm_config.tool_visible(t)
    ]
    if bm_tools.BERSERK_MCP_DISCOVERY:
        allt = [t for t in allt if t["name"] in bm_tools._ANCHOR_TOOL_NAMES]
    tl = []
    hidden = bm_tools._hidden_tool_names()
    builtin = {t["name"] for t in bm_tools.TOOLS + bm_tools.MGMT_TOOLS}
    for t in allt:
        visible_tool = bm_tools._with_output_schema(t) if mode == bm_config.PROTOCOL_MODE_MODERN else t
        description = visible_tool["description"]
        if visible_tool["name"] in builtin:
            # saved__* descriptions are fenced user/model text; never edit them.
            description = bm_tools._without_hidden_tool_sentences(description, hidden)
        item = {
            "name": visible_tool["name"],
            "title": bm_tools.TITLES.get(visible_tool["name"], visible_tool["name"]),
            "description": description,
            "inputSchema": visible_tool["inputSchema"],
            "annotations": bm_tools.annotations_for(visible_tool["name"]),
        }
        if "outputSchema" in visible_tool:
            item["outputSchema"] = visible_tool["outputSchema"]
        tl.append(item)
    result = {"tools": tl}
    if mode == bm_config.PROTOCOL_MODE_MODERN:
        result["resultType"] = "complete"
        result["ttlMs"] = bm_config.MCP_PRIVATE_CACHE_TTL_MS
        result["cacheScope"] = "private"
    return result


def _extract_structured_content(name, text, is_error):
    if is_error or name not in bm_tools._STRUCTURED_OUTPUT_TOOLS:
        return None
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("schema_version"), str):
        return payload
    return None


def _input_required_result(reason, message, request_state, input_requests=None):
    result = {
        "resultType": "input_required",
        "reason": reason,
        "content": [{"type": "text", "text": message}],
        "requestState": json.dumps(request_state, sort_keys=True, separators=(",", ":")),
    }
    if input_requests:
        result["inputRequests"] = input_requests
    return result


def _looks_bounded_kql(kql):
    lowered = str(kql or "").lower()
    return any(
        re.search(r"\b" + re.escape(operator) + r"\b", lowered)
        for operator in ("take", "limit", "count", "summarize", "top")
    )


def _modern_preflight_input_required(name, arguments):
    if name == "search":
        kql = str(arguments.get("kql") or "")
        since = arguments.get("since") or "15m ago"
        if (
            bm_runner.valid_since(since)
            and bm_config._since_hours(since) > bm_config.MCP_EXPENSIVE_SEARCH_WINDOW_HOURS
            and not _looks_bounded_kql(kql)
            and arguments.get("allow_expensive") is not True
        ):
            message = (
                "This custom KQL spans more than 24 hours and does not appear "
                "to include a bounding operator such as take, limit, count, "
                "summarize, or top. Narrow the window, add a bounding operator, "
                "or retry with arguments.allow_expensive=true if this cost is "
                "intentional."
            )
            return _input_required_result(
                "expensive_query_guard",
                message,
                {
                    "tool": name,
                    "since": since,
                    "window_hours": bm_config._since_hours(since),
                    "suggested_actions": [
                        "narrow since to 24h ago or less",
                        "add take/limit/count/summarize/top",
                        "retry with allow_expensive=true after explicit approval",
                    ],
                },
            )
    if name == "claude_feature_cost" and not str(arguments.get("feature_id") or "").strip():
        return _input_required_result(
            "missing_finops_attribution",
            "Feature economics requires a feature_id so spend can be attributed without guessing.",
            {
                "tool": name,
                "missing": ["feature_id"],
                "suggested_actions": ["provide feature_id"],
            },
        )
    if name == "claude_project_economics" and not str(arguments.get("project_id") or "").strip():
        return _input_required_result(
            "missing_finops_attribution",
            "Project economics requires a project_id so spend can be attributed without guessing.",
            {
                "tool": name,
                "missing": ["project_id"],
                "suggested_actions": ["provide project_id"],
            },
        )
    return None


def _tool_call_result(name, text, is_error, mode):
    result = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if mode == bm_config.PROTOCOL_MODE_MODERN:
        result["resultType"] = "complete"
        structured = _extract_structured_content(name, text, is_error)
        if structured is not None:
            result["structuredContent"] = structured
    return result


def dispatch(req):
    """Handle one JSON-RPC request per JSON-RPC 2.0 and MCP 2025-06-18.

    Returns a response dict, or None for valid notifications.
    """
    if not isinstance(req, dict):
        return _jsonrpc_error(-32600, "Invalid Request")

    if req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
        return _jsonrpc_error(-32600, "Invalid Request")

    has_id = "id" in req
    id_ = req.get("id")
    if has_id and not _valid_mcp_id(id_):
        return _jsonrpc_error(-32600, "Invalid Request")

    is_notification = not has_id
    method = req["method"]

    if "params" in req and not isinstance(req["params"], dict):
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)

    params = req.get("params") or {}

    try:
        mode = _protocol_mode_for_request(method, params)
        _note_request_mode(mode)
        return _dispatch_validated(method, params, id_, is_notification, mode=mode)
    except Exception as exc:
        bm_config.log(f"dispatch failed: {type(exc).__name__}")
        if is_notification:
            return None
        return _jsonrpc_error(-32603, "Internal error", id_)


def _dispatch_discover(params, id_, mode):
    """Handle server/discover. Returns response."""
    if mode != bm_config.PROTOCOL_MODE_MODERN:
        if _modern_mcp_enabled() and _request_meta(params) is None:
            return _jsonrpc_error(-32602, "Invalid params", id_)
        requested = _requested_protocol_version(params)
        if _modern_mcp_enabled() and requested and requested not in bm_config.SUPPORTED_PROTOCOL_VERSIONS:
            return _jsonrpc_unsupported_protocol(id_, requested)
        if _modern_mcp_enabled() and requested != bm_config.MCP_PROTOCOL_MODERN:
            return _jsonrpc_error(-32602, "Invalid params", id_)
        return _jsonrpc_error(-32601, "Method not found", id_)
    if set(params) - {"_meta"}:
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if not _valid_modern_meta(params):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    return _jsonrpc_result(id_, _discover_result())


def _dispatch_tasks(method, params, id_, mode):
    """Handle tasks/get and tasks/cancel. Returns response."""
    if mode != bm_config.PROTOCOL_MODE_MODERN:
        return _jsonrpc_error(-32601, "Method not found", id_)
    if set(params) - {"_meta", "taskId", "id"}:
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if not _valid_modern_meta(params):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    task_id = _task_id_from_params(params)
    if task_id is None:
        return _jsonrpc_error(-32602, "Invalid params", id_)
    record = _task_lookup(task_id)
    if record is None:
        return _jsonrpc_error(-32602, "Unknown task", id_)
    if method == "tasks/cancel":
        with _TASK_LOCK:
            current = _TASKS.get(task_id)
            if current is None or current.get("role") != bm_config.ACTIVE_ROLE:
                return _jsonrpc_error(-32602, "Unknown task", id_)
            if current.get("status") in {"pending", "running"}:
                current["status"] = "cancelled"
                current["updated_ts"] = _task_now()
                current["updated_at"] = bm_config.now_iso()
            record = dict(current)
    return _jsonrpc_result(id_, _task_result(record))


def _dispatch_protocol(method, params, id_, is_notification, mode):
    """Handle MCP protocol methods (discover, tasks, initialize, ping). Returns response or NOT_MATCHED."""
    if method == "server/discover":
        if is_notification:
            return None
        return _dispatch_discover(params, id_, mode)
    if method in {"tasks/get", "tasks/cancel"}:
        if is_notification:
            return None
        return _dispatch_tasks(method, params, id_, mode)
    if method == "initialize":
        if is_notification:
            return None
        pv = params.get("protocolVersion")
        if not isinstance(pv, str) or not pv.strip():
            return _jsonrpc_error(-32602, "Invalid params", id_)
        caps = params.get("capabilities")
        if caps is not None and not isinstance(caps, dict):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        client_info = params.get("clientInfo")
        if client_info is not None and not isinstance(client_info, dict):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        return _jsonrpc_result(
            id_,
            {
                "protocolVersion": bm_config.PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": _list_changed_supported()}},
                "serverInfo": bm_config.SERVER_INFO,
                "instructions": bm_config.INSTRUCTIONS,
            },
        )
    if method == "notifications/initialized":
        if not is_notification:
            return _jsonrpc_error(-32600, "Invalid Request", id_)
        return None
    if method == "ping":
        if params:
            if is_notification:
                return None
            return _jsonrpc_error(-32602, "Invalid params", id_)
        if is_notification:
            return None
        return _jsonrpc_result(id_, {})

    return "NOT_MATCHED"


def _dispatch_tools_list(params, id_, is_notification, mode):
    """Handle tools/list. Returns response or None."""
    if set(params) - {"_meta", "cursor"}:
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if "_meta" in params and not isinstance(params["_meta"], dict):
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if params.get("cursor") is not None:
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if mode == bm_config.PROTOCOL_MODE_MODERN and not _valid_modern_meta(params):
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if is_notification:
        return None
    return _jsonrpc_result(id_, _tool_list_result(mode))


# Argument names every tool accepts although no inputSchema declares them:
# protocol-level keys read before handle_call (modern task creation, and the
# cost-approval retry after an input_required preflight).
_PROTOCOL_ARGUMENTS = frozenset({"as_task", "allow_expensive"})


_MAX_REPORTED_ARGUMENT_NAMES = 5


_MAX_REPORTED_ARGUMENT_CHARS = 64


def _unknown_argument_error(tool, arguments):
    """Error text when `arguments` holds a name the tool's schema does not
    declare, else None.

    Review 2026-09-26 P2: a misspelled optional filter (`svc` for `service`)
    used to be ignored, so the call ran unfiltered and the answer looked
    filtered. Rejecting the name and listing the valid ones lets the model
    correct itself. Caller-supplied names are bounded before being echoed.
    """
    declared = set(tool.get("inputSchema", {}).get("properties", {}))
    unknown = sorted(name for name in arguments if name not in declared and name not in _PROTOCOL_ARGUMENTS)
    if not unknown:
        return None
    shown = [repr(name[:_MAX_REPORTED_ARGUMENT_CHARS]) for name in unknown[:_MAX_REPORTED_ARGUMENT_NAMES]]
    if len(unknown) > _MAX_REPORTED_ARGUMENT_NAMES:
        shown.append(f"and {len(unknown) - _MAX_REPORTED_ARGUMENT_NAMES} more")
    valid = ", ".join(sorted(declared)) or "(none)"
    label = "argument" if len(unknown) == 1 else "arguments"
    return f"unknown {label} {', '.join(shown)} for {tool['name']}; valid: {valid}"


def _dispatch_tools_call(params, id_, mode):
    """Handle tools/call (never a notification). Returns response."""
    if mode == bm_config.PROTOCOL_MODE_MODERN and not _valid_modern_meta(params):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    name = params.get("name")
    if not name or not isinstance(name, str):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    arguments = arguments or {}
    bm_runner._normalize_since_arg(arguments)
    matched_tool = next((t for t in bm_tools.TOOLS + bm_tools.MGMT_TOOLS if t["name"] == name), None)
    if matched_tool is None and name.startswith("saved__"):
        matched_tool = next((t for t in bm_learned._saved_query_tools() if t["name"] == name), None)
    # Visibility first: a hidden tool must answer "unknown tool" whatever its
    # arguments, or the argument error below would reveal its schema.
    argument_error = None
    if matched_tool is not None and bm_config.tool_visible(matched_tool):
        argument_error = _unknown_argument_error(matched_tool, arguments)
    if matched_tool is not None and not bm_config.tool_visible(matched_tool):
        text, is_err = "unknown tool: " + name, True
    elif argument_error is not None:
        text, is_err = argument_error, True
    else:
        if mode == bm_config.PROTOCOL_MODE_MODERN:
            input_required = _modern_preflight_input_required(name, arguments)
            if input_required is not None:
                return _jsonrpc_result(id_, input_required)
            if (
                name in bm_tools._TASK_ELIGIBLE_TOOLS
                and arguments.get("as_task") is True
                and _client_supports_tasks(params)
            ):
                task_args = dict(arguments)
                task_args.pop("as_task", None)
                task_result = _create_task(name, task_args, mode)
                if task_result is None:
                    return _jsonrpc_error(-32000, "Task limit reached", id_)
                return _jsonrpc_result(id_, task_result)
        text, is_err = bm_dispatch.handle_call(name, arguments)
    text = secret_scan.apply_output_filter(
        text,
        mode=bm_config.REDACT_MODE,
        include_entropy=bm_config.REDACT_ENTROPY,
        pii_types=bm_config.REDACT_PII_TYPES,
    )
    return _jsonrpc_result(id_, _tool_call_result(name, text, is_err, mode))


def _dispatch_validated(method, params, id_, is_notification, mode=bm_config.PROTOCOL_MODE_LEGACY):
    """Dispatch a validated request envelope to the appropriate handler."""
    result = _dispatch_protocol(method, params, id_, is_notification, mode)
    if result != "NOT_MATCHED":
        return result
    if method == "subscriptions/listen":
        return _dispatch_listen(params, id_, is_notification, mode)
    if method == "notifications/cancelled" and is_notification:
        request_id = params.get("requestId")
        if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
            with _LISTEN_LOCK:
                _LISTEN_SUBSCRIPTIONS.pop(request_id, None)
        return None
    if method == "tools/list":
        return _dispatch_tools_list(params, id_, is_notification, mode)
    if method == "tools/call":
        if is_notification:
            return None
        return _dispatch_tools_call(params, id_, mode)
    if is_notification:
        return None
    return _jsonrpc_error(-32601, "Method not found", id_)


_SEND_LOCK = threading.Lock()


def send(msg):
    # Task threads send notifications while the main loop sends responses; one
    # lock keeps each JSON line whole on the shared stdout.
    line = json.dumps(msg) + "\n"
    with _SEND_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


# Which transport is serving this process, set by _serve_mcp()/_serve_http()
# -- None until one of them starts (e.g. under test, or during import).
# Governs whether tools.listChanged is advertised truthfully: stdio can push
# a notification at any time via send(); HTTP's do_POST is one request, one
# response, with no server-initiated channel -- advertising listChanged
# there would promise a notification that can never arrive.
_TRANSPORT = None


def _serve_mcp():
    global _TRANSPORT
    _TRANSPORT = "stdio"
    bm_config.log(
        f"starting v{__version__} (profile={bm_config.PROFILE}, table={bm_config.TABLE}, bzrk={bm_config.BZRK_BIN})"
    )
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            bm_config.log(f"bad json from client ({type(e).__name__})")
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        try:
            resp = dispatch(req)
        except Exception as e:  # pragma: no cover - defense in depth
            bm_config.log(f"dispatch crashed: {type(e).__name__}")
            if isinstance(req, dict) and "id" in req and _valid_mcp_id(req["id"]):
                resp = _jsonrpc_error(-32603, "Internal error", req["id"])
            else:
                continue
        if resp is not None:
            send(resp)
    bm_config.log("stdin closed")


def _http_error(handler, status, message):
    body = json.dumps({"error": message}).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


_REFUSED_BODY_DRAIN_TIMEOUT = 2.0


def _discard_request_body(handler, cap):
    """Read and drop a refused request's body, up to `cap` bytes, before
    replying. Closing a socket with unread data makes Windows send a TCP reset,
    so the client could see "connection aborted" instead of the error status.
    The request is not yet authorised, so the drain stops at an absolute
    deadline: a client that trickles bytes cannot hold the handler thread."""
    try:
        remaining = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return
    if not 0 < remaining <= cap:
        return
    sock = handler.connection
    previous = sock.gettimeout()
    deadline = time.monotonic() + _REFUSED_BODY_DRAIN_TIMEOUT
    try:
        while remaining > 0:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            chunk = handler.rfile.read1(min(remaining, 64 * 1024))
            if not chunk:
                break
            remaining -= len(chunk)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            sock.settimeout(previous)


def _http_refuse(handler, config, status, message):
    """Send an error for a request whose body was never read."""
    _discard_request_body(handler, config["max_request_bytes"])
    _http_error(handler, status, message)


def _make_http_handler(config):
    class BerserkMcpHttpHandler(BaseHTTPRequestHandler):
        server_version = "berserk-mcp"
        sys_version = ""

        def log_message(self, fmt, *args):
            bm_config.log("http: " + bm_config._sanitize_log_line(fmt % args))

        def do_GET(self):
            if self.path != "/healthz":
                _http_error(self, 404, "not found")
                return
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            _http_error(self, 405, "method not allowed")

        def do_POST(self):
            if self.path != "/mcp":
                _http_refuse(self, config, 404, "not found")
                return
            ok, status, message = bm_httpconfig._http_request_allowed(self, config)
            if not ok:
                _http_refuse(self, config, status, message)
                return
            ctype = self.headers.get("Content-Type", "")
            if "application/json" not in ctype.lower():
                _http_refuse(self, config, 415, "content-type must be application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                _http_error(self, 411, "content-length required")
                return
            if length < 0 or length > config["max_request_bytes"]:
                _http_error(self, 413, "request too large")
                return
            if not config["semaphore"].acquire(blocking=False):
                _http_refuse(self, config, 429, "too many concurrent requests")
                return
            try:
                raw = self.rfile.read(length)
                try:
                    req = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
                else:
                    resp = dispatch(req)
                    if resp is None:
                        self.send_response(204)
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        return
                body = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            finally:
                config["semaphore"].release()

    return BerserkMcpHttpHandler


def _serve_http():
    global _TRANSPORT
    _TRANSPORT = "http"
    config = bm_httpconfig._build_http_config()
    if not config["enabled"]:
        raise bm_httpconfig.HttpConfigError("HTTP transport is disabled; set BERSERK_MCP_HTTP_ENABLE=1")
    handler = _make_http_handler(config)
    bm_config.log(f"http starting v{__version__} on {config['host']}:{config['port']}")
    server = ThreadingHTTPServer((config["host"], config["port"]), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()

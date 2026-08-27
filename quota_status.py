"""Live quota-window status for Claude Code (issue #43).

Two paths, always graceful -- this module must never raise out to its
caller, and must never require a daemon or forwarder to be running:

1. Live: read the OAuth token Claude Code itself stores in the macOS
   Keychain, call Anthropic's usage endpoint with it. Precise and
   real-time, macOS-only. This is an UNDOCUMENTED, internal endpoint, not
   part of Anthropic's public API -- its response shape is NOT verified
   against real output (permission to test against a live Keychain token
   was denied by this environment's auto-mode classifier; see issue #43's
   discussion). Every field access on the live response is therefore
   defensive and optional -- never assume a key exists, never raise on an
   unexpected shape.

2. Fallback: sum tokens already ingested via existing OTel telemetry over
   the trailing window (agent_analytics.total_tokens_estimate). Works
   everywhere, needs no local Keychain access, and is always used when the
   live path fails for any reason -- unreachable endpoint, no Keychain
   entry, non-macOS, malformed response.

The caller is always told which path produced the result via the "source"
field: "live", "estimated", or "unavailable".
"""
import json
import os
import platform
import subprocess
import urllib.error
import urllib.request

import agent_analytics
import _http

KEYCHAIN_SERVICE = os.environ.get("BERSERK_MCP_QUOTA_KEYCHAIN_SERVICE", "Claude Code-credentials")
USAGE_ENDPOINT = os.environ.get("BERSERK_MCP_QUOTA_ENDPOINT", "https://api.anthropic.com/api/oauth/usage")
_KEYCHAIN_TIMEOUT_S = 5
_HTTP_TIMEOUT_S = 5

# Fields optimistically extracted from the live endpoint if present. This
# list is a best guess, not a verified contract -- see the module
# docstring. Extend it once a real response is confirmed.
_LIVE_FIELDS = (
    "utilization", "five_hour_utilization", "seven_day_utilization",
    "resets_at", "rate_limit_tier",
)


def _read_oauth_token(run=subprocess.run, platform_name=None):
    """Returns the access token string from the macOS Keychain, or None on
    any failure -- never raises. macOS only (the `security` CLI doesn't
    exist elsewhere)."""
    if (platform_name if platform_name is not None else platform.system()) != "Darwin":
        return None
    try:
        result = run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=_KEYCHAIN_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        blob = json.loads(result.stdout)
    except ValueError:
        return None
    oauth = blob.get("claudeAiOauth") if isinstance(blob, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def _fetch_live_usage(token, opener=_http.NO_REDIRECT_OPENER.open):
    """Calls the (undocumented, unstable) usage endpoint. Returns the
    parsed JSON dict, or None on ANY failure -- network error, non-200,
    unexpected body shape, or a blocked redirect. Never raises.

    Uses the shared no-redirect opener (SEC-04, Codex security review):
    plain urlopen follows redirects and re-sends this request's
    Authorization: Bearer <oauth token> header to whatever host the
    redirect names. A redirect is treated the same as any other failure --
    degrade to the log-derived fallback, never forward the token onward."""
    req = urllib.request.Request(
        USAGE_ENDPOINT,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with opener(req, timeout=_HTTP_TIMEOUT_S) as resp:
            status = getattr(resp, "status", None)
            if status is None and hasattr(resp, "getcode"):
                status = resp.getcode()
            if status != 200:
                return None
            body = resp.read()
    except Exception as exc:
        # Defensive by design: the live endpoint is undocumented and
        # unverified (see module docstring). Any failure here -- network,
        # TLS, timeout, a blocked redirect, or something not yet seen --
        # must degrade to the log-derived fallback, never propagate.
        close = getattr(exc, "close", None)
        if callable(close):
            close()
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _extract_live_fields(data):
    """Best-effort extraction from an unverified response shape. Every
    field is optional; returns a dict with whatever was present."""
    out = {}
    if not isinstance(data, dict):
        return out
    for key in _LIVE_FIELDS:
        if key in data:
            out[key] = data[key]
    return out


def get_quota_status(since="5h ago", run=subprocess.run, opener=_http.NO_REDIRECT_OPENER.open,
                      platform_name=None, _total_tokens_estimate=agent_analytics.total_tokens_estimate):
    """Main entry point. Tries the live Keychain+endpoint path first;
    falls back to log-derived estimation on ANY failure. Never raises,
    never requires a daemon/forwarder to be running. Returns a dict with
    at least {"source": "live"|"estimated"|"unavailable", "ok": bool}."""
    token = _read_oauth_token(run=run, platform_name=platform_name)
    if token:
        live = _fetch_live_usage(token, opener=opener)
        if live is not None:
            return {"source": "live", "ok": True, **_extract_live_fields(live)}

    total, all_exact, is_err = _total_tokens_estimate(since)
    if is_err or total is None:
        return {
            "source": "unavailable", "ok": False,
            "detail": "live endpoint unavailable and the telemetry query failed",
        }
    return {
        "source": "estimated", "ok": True, "since": since,
        "total_tokens": total, "all_exact": all_exact,
    }


def format_quota_status(result):
    """Render get_quota_status()'s dict as the human-readable text every
    other claude_* tool returns."""
    source = result.get("source")
    if source == "live":
        fields = {k: v for k, v in result.items() if k not in ("source", "ok")}
        if not fields:
            return (
                "Live quota status: endpoint reachable but returned no recognized "
                "usage fields (unverified response shape -- see issue #43)."
            )
        lines = ["Live quota status (from Anthropic's account usage endpoint):"]
        for k, v in fields.items():
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)
    if source == "estimated":
        exact_note = "exact usage" if result.get("all_exact") else "includes body-length estimates for some sessions"
        return (
            f"Estimated quota usage over {result.get('since')} "
            f"(log-derived, live endpoint unavailable -- {exact_note}): "
            f"{result.get('total_tokens')} tokens.\n"
            "This is a retrospective estimate from ingested telemetry, not a live account reading."
        )
    return (
        "Quota status unavailable: the live endpoint could not be reached "
        f"and the fallback telemetry query failed ({result.get('detail', 'unknown error')})."
    )

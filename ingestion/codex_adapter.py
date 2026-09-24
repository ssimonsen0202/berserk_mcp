"""Ingestion adapter: Codex CLI session rollouts -> Berserk OTLP (issue #42).

Codex stores per-session transcripts as JSONL under ~/.codex/sessions/ and
~/.codex/archived_sessions/ (filename: rollout-<timestamp>-<session-uuid>.jsonl),
one line per event, each shaped {"timestamp", "type", "payload"}. This is a
richer, differently-organized format than Claude Code's own transcripts --
notably token usage (including a live quota "used_percent") arrives as its
own event_msg/token_count record rather than being attached to message
events, and there is no single "assistant message" event type the way Claude
Code has one -- tool calls, tool results, and text messages are all separate
event types.

First-pass scope, deliberately narrow rather than attempting full parity in
one PR: token usage (token_count), tool calls (function_call), tool results
(function_call_output -- recorded, but error detection is NOT yet attempted;
Codex's success/failure convention for these wasn't conclusively confirmed
from the sample data inspected), and user messages (user_message). Every
other event type (reasoning, custom_tool_call, mcp_tool_call_end, world_state,
session_meta, turn_context, ...) is intentionally skipped for now -- known
follow-up scope, not an oversight.

Records are tagged resource['service.name'] = "codex-cli" so they land
alongside, not mixed into, existing Claude Code data (see
berserk_mcp.py's _AGENT_SERVICE_NAMES).
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import _http  # noqa: E402
import _store  # noqa: E402
import secret_scan  # noqa: E402

SERVICE_NAME = "codex-cli"

_SESSION_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def redact_text(text):
    """Redact with the project's shared redactor (secret patterns, quoted
    credentials, high-entropy runs, including Fernet-shaped payloads).
    Returns (redacted, count)."""
    clean, findings = secret_scan.redact(text, include_entropy=True, pii_types=())
    return clean, sum(f["count"] for f in findings)


def parse_codex_line(raw_line):
    """Normalize one Codex rollout JSONL line into a flat record dict, or
    None if this line's type isn't mapped (see module docstring for scope)."""
    raw_line = (raw_line or "").strip()
    if not raw_line:
        return None
    try:
        d = json.loads(raw_line)
    except ValueError:
        return None

    top_type = d.get("type")
    payload = d.get("payload")
    if not isinstance(payload, dict):
        return None
    payload_type = payload.get("type")

    if top_type == "event_msg" and payload_type == "token_count":
        info = payload.get("info") or {}
        usage = info.get("last_token_usage") or {}
        rec = {
            "type": "token_count",
            "input_tokens": usage.get("input_tokens", 0) or 0,
            "cached_input_tokens": usage.get("cached_input_tokens", 0) or 0,
            "output_tokens": usage.get("output_tokens", 0) or 0,
            "total_tokens": usage.get("total_tokens", 0) or 0,
        }
        rate_limits = payload.get("rate_limits") or {}
        primary = rate_limits.get("primary") or {}
        if "used_percent" in primary:
            rec["quota_used_percent"] = primary["used_percent"]
        return rec

    if top_type == "response_item" and payload_type == "function_call":
        name = payload.get("name")
        if not name:
            return None
        return {"type": "tool_call", "tool_names": name}

    if top_type == "response_item" and payload_type == "function_call_output":
        return {"type": "tool_result"}

    if top_type == "event_msg" and payload_type == "user_message":
        message = payload.get("message")
        if message is None:
            return None
        return {"type": "user", "body": str(message)}

    return None


def extract_session_id_from_line(raw_line):
    """session_id from a session_meta line, or None for any other line type."""
    try:
        d = json.loads((raw_line or "").strip() or "{}")
    except ValueError:
        return None
    if d.get("type") != "session_meta":
        return None
    payload = d.get("payload") or {}
    return payload.get("session_id") or payload.get("id")


def extract_session_id_from_filename(path):
    """Fallback: every rollout filename embeds the session UUID."""
    m = _SESSION_UUID_RE.search(Path(path).name)
    return m.group(1) if m else None


def iter_rollout_files(codex_home=None):
    home = Path(codex_home or os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    patterns = [
        str(home / "sessions" / "**" / "*.jsonl"),
        str(home / "archived_sessions" / "*.jsonl"),
    ]
    seen = set()
    for pattern in patterns:
        for f in sorted(glob.glob(pattern, recursive=True)):
            if f not in seen:
                seen.add(f)
                yield Path(f)


def build_otlp_record(rec, session_id, hostname):
    attrs = {
        "claude.type": rec["type"],
        "claude.session_id": session_id or "unknown",
    }
    if "tool_names" in rec:
        attrs["claude.tool_names"] = rec["tool_names"]
    body = None
    if rec["type"] == "user":
        body, _ = redact_text(rec.get("body", ""))
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens", "quota_used_percent"):
        if key in rec:
            attrs[f"codex.{key}"] = rec[key]

    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": SERVICE_NAME}},
                        {"key": "host.name", "value": {"stringValue": hostname}},
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": str(int(time.time() * 1e9)),
                                "body": {"stringValue": body} if body is not None else {"stringValue": ""},
                                "attributes": [{"key": k, "value": _otlp_value(v)} for k, v in attrs.items()],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _otlp_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _state_path(state_dir):
    """Validated before any read or write: absolute, traversal-free, control-free."""
    return _store.validate_store_path(Path(state_dir) / "codex_adapter_state.json", purpose="codex adapter state")


def load_state(state_dir):
    return _store.load_json_dict(_state_path(state_dir))


def save_state(state_dir, state):
    # Private store: directories it creates are 0700, the file 0600, replaced atomically.
    _store.atomic_write_json(_state_path(state_dir), state, private=True, purpose="codex adapter state")


def process_file(path, offset, session_id_cache):
    """Read complete lines since `offset`, return (records, new_offset, session_id).
    session_id is resolved once per file (session_meta line, else filename)
    and cached across calls since a file's session_id never changes.

    Codex may still be appending to this file (an active session's rollout
    is written live). Only bytes up to and including the last newline are
    ever considered "read" -- a trailing partial line stays unconsumed so
    the offset can never land mid-record. Reading past a partial line here
    would permanently split that record across two runs: its first half
    parsed (and dropped, since it's incomplete JSON) now, its second half
    misread as a corrupt fragment next run."""
    session_id = session_id_cache.get(str(path))
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()

    complete_end = data.rfind(b"\n")
    if complete_end == -1:
        return [], offset, session_id or extract_session_id_from_filename(path)

    new_offset = offset + complete_end + 1
    records = []
    for raw in data[:complete_end].split(b"\n"):
        if not raw:
            continue
        line = raw.decode("utf-8", "replace")
        if session_id is None:
            session_id = extract_session_id_from_line(line)
        rec = parse_codex_line(line)
        if rec is not None:
            records.append(rec)
    if session_id is None:
        session_id = extract_session_id_from_filename(path)
    return records, new_offset, session_id


def run(codex_home, state_dir, otlp_endpoint, otlp_bearer, hostname, dry_run=False):
    state = load_state(state_dir)
    offsets = state.setdefault("offsets", {})
    session_ids = state.setdefault("session_ids", {})

    total_emitted = 0
    for path in iter_rollout_files(codex_home):
        key = str(path)
        offset = offsets.get(key, 0)
        try:
            current_size = path.stat().st_size
        except OSError:
            continue
        if current_size < offset:
            offset = 0  # file rotated/truncated
        if current_size == offset:
            continue

        try:
            records, new_offset, session_id = process_file(path, offset, session_ids)
        except OSError as exc:
            # File vanished/rotated between the stat() above and here (a real
            # race with Codex archiving a session). Skip it this run rather
            # than crashing the whole batch -- state saved below still
            # reflects every other file processed so far in this run.
            sys.stderr.write(f"codex_adapter: skipping {path}: {exc}\n")
            continue
        if session_id:
            session_ids[key] = session_id

        file_had_failure = False
        for rec in records:
            payload = build_otlp_record(rec, session_id, hostname)
            if dry_run:
                print(json.dumps(payload))
                total_emitted += 1
                continue
            headers = {"Content-Type": "application/json"}
            if otlp_bearer:
                headers["Authorization"] = f"Bearer {otlp_bearer}"
            _, err = _http.http_post_json(otlp_endpoint, headers, payload, timeout=30, allow_plaintext_remote=False)
            if err:
                sys.stderr.write(f"codex_adapter: OTLP post failed: {err}\n")
                file_had_failure = True
                continue
            total_emitted += 1

        # Only advance past what actually made it to Berserk. A partial
        # failure mid-file leaves the offset where it was, so the whole
        # file (including the records that did succeed) is retried next
        # run -- a possible duplicate POST, never a silent drop.
        if not file_had_failure:
            offsets[key] = new_offset

    if not dry_run:
        save_state(state_dir, state)
    return total_emitted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=None, help="default: $CODEX_HOME or ~/.codex")
    parser.add_argument("--state-dir", default=str(Path.home() / ".berserk" / "codex_adapter"))
    parser.add_argument("--otlp-endpoint", default=os.environ.get("BERSERK_MCP_OTLP_LOGS_ENDPOINT", ""))
    parser.add_argument("--otlp-bearer-env", default="", help="env var holding the OTLP bearer token")
    parser.add_argument("--hostname", default=os.uname().nodename if hasattr(os, "uname") else "unknown")
    parser.add_argument("--dry-run", action="store_true", help="print OTLP payloads instead of POSTing")
    args = parser.parse_args(argv)

    if not args.dry_run and not args.otlp_endpoint:
        sys.stderr.write(
            "refusing to start: no --otlp-endpoint given and BERSERK_MCP_OTLP_LOGS_ENDPOINT is unset. Use --dry-run to test without one.\n"
        )
        sys.exit(1)

    try:
        _state_path(args.state_dir)
    except _store.StorePathError as exc:
        sys.stderr.write(f"refusing to start: {exc}\n")
        sys.exit(2)
    bearer = os.environ.get(args.otlp_bearer_env) if args.otlp_bearer_env else None
    n = run(args.codex_home, args.state_dir, args.otlp_endpoint, bearer, args.hostname, dry_run=args.dry_run)
    print(f"codex_adapter: emitted {n} record(s)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Extract full-fidelity MCP tool-call records from Hermes session logs.

Scans ~/.hermes/sessions/*.json (Hermes's own per-run transcript store) and
emits one JSON object per tool call — matching each assistant tool_calls
entry to its tool-role result via tool_call_id — with complete, untruncated
arguments/results plus the model and session that made the call.

Usage:
    hermes_tool_call_log.py [server ...]

    # all tool calls to the 'berserk' and 'berserk-q' MCP servers (default)
    hermes_tool_call_log.py

    # only 'berserk'
    hermes_tool_call_log.py berserk

    # every MCP tool call regardless of server
    hermes_tool_call_log.py --all

Output is newline-delimited JSON (one record per line), oldest session first,
so it composes with grep/jq without any lossy summarization.
"""
import json
import sys
from pathlib import Path

SESSIONS_DIR = Path.home() / ".hermes" / "sessions"


def matches(tool_name, server_filter):
    if server_filter is None:
        return True
    return any(tool_name.startswith(f"mcp_{s}_") for s in server_filter)


def extract_tool_calls(session_path, server_filter):
    data = json.loads(session_path.read_text())
    model = data.get("model")
    session_id = data.get("session_id")
    session_start = data.get("session_start")

    pending = {}
    records = []
    for msg in data.get("messages", []):
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            for call in msg["tool_calls"]:
                fn = call.get("function", {})
                name = fn.get("name", "")
                if not matches(name, server_filter):
                    continue
                pending[call.get("id")] = {
                    "session_id": session_id,
                    "session_start": session_start,
                    "model": model,
                    "tool_name": name,
                    "arguments": fn.get("arguments"),
                }
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id in pending:
                rec = pending.pop(call_id)
                rec["result"] = msg.get("content")
                records.append(rec)

    # tool calls whose session ended before a result came back (crash/timeout)
    for rec in pending.values():
        rec["result"] = None
        records.append(rec)
    return records


def main():
    args = sys.argv[1:]
    server_filter = None if args == ["--all"] else (args or ["berserk", "berserk-q"])

    for path in sorted(SESSIONS_DIR.glob("session_*.json")):
        try:
            records = extract_tool_calls(path, server_filter)
        except (json.JSONDecodeError, OSError) as e:
            print(f"# skipped {path.name}: {e}", file=sys.stderr)
            continue
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))


if __name__ == "__main__":
    main()

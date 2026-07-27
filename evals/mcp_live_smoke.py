#!/usr/bin/env python3
"""Read-only live smoke eval for a deployed/local berserk-mcp server.

This launches an MCP server over stdio, performs the MCP handshake, and executes
safe read-only tool calls. It is intended for post-deploy validation against a
real Berserk profile. It does not create dashboards, write recommendation
decisions, save queries, or mutate Berserk.

Examples:
  BERSERK_MCP_ROLE=claude python evals/mcp_live_smoke.py
  python evals/mcp_live_smoke.py --server-command berserk-mcp --since "30d ago"
  python evals/mcp_live_smoke.py --json-out /tmp/berserk-mcp-live-smoke.json
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = [sys.executable, str(ROOT / "berserk_mcp.py")]

EXPECTED_CLAUDE_TOOLS = {
    "validate_kql",
    "claude_recent",
    "claude_sessions",
    "claude_tools",
    "claude_errors",
    "claude_search",
    "claude_loop_check",
    "claude_model_fit",
    "claude_token_burn",
    "claude_cost_report",
    "claude_session_deep_dive",
    "claude_workflow_insights",
    "claude_spend_overview",
    "claude_feature_cost",
    "claude_project_economics",
    "claude_efficiency_insights",
    "claude_harness_recommendations",
    "claude_optimization_impact",
    "claude_management_report",
}

READ_ONLY_CALLS = [
    ("validate_kql_good", "validate_kql", {"kql": "default | take 5", "mode": "static"}),
    ("validate_kql_semicolon", "validate_kql", {"kql": "default | take 1; .show tables", "mode": "static"}),
    ("validate_kql_source_operator", "validate_kql", {"kql": "default | union default | take 5", "mode": "static"}),
    ("claude_recent", "claude_recent", {"since": "6h ago"}),
    ("claude_sessions", "claude_sessions", {"since": "6h ago"}),
    ("claude_tools", "claude_tools", {"since": "6h ago"}),
    ("claude_errors", "claude_errors", {"since": "6h ago"}),
    ("claude_token_burn", "claude_token_burn", {"since": "7d ago"}),
    ("claude_cost_report_day", "claude_cost_report", {"since": "7d ago", "group_by": "day"}),
    ("claude_spend_overview_day", "claude_spend_overview", {"since": "30d ago", "group_by": "day"}),
    ("claude_efficiency_insights", "claude_efficiency_insights", {"since": "30d ago"}),
    ("claude_harness_recommendations", "claude_harness_recommendations", {"since": "30d ago"}),
    ("claude_management_report", "claude_management_report", {"scope": "portfolio", "since": "30d ago"}),
]

SENSITIVE_MARKERS = (
    "sk-",
    "AKIA",
    "BEGIN PRIVATE KEY",
    "password=",
    "token=",
)


class McpClient:
    def __init__(self, command, env=None, timeout=60):
        self.command = command
        self.timeout = timeout
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self.next_id = 1

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def recv(self):
        line = self.proc.stdout.readline()
        if not line:
            stderr = ""
            try:
                _, stderr = self.proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                _, stderr = self.proc.communicate(timeout=2)
            raise RuntimeError("MCP server exited: " + str(stderr or "").strip()[:2000])
        return json.loads(line)

    def request(self, method, params=None):
        req_id = self.next_id
        self.next_id += 1
        obj = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            obj["params"] = params
        self.send(obj)
        return self.recv()

    def notify(self, method, params=None):
        obj = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            obj["params"] = params
        self.send(obj)

    def initialize(self):
        return self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "berserk-mcp-live-smoke", "version": "1"},
            },
        )

    def list_tools(self):
        return self.request("tools/list").get("result", {}).get("tools", [])

    def call_tool(self, name, arguments):
        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )


def content_text(response):
    result = response.get("result") or {}
    parts = result.get("content") or []
    return "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text")


def result_is_error(response):
    if "error" in response:
        return True
    return bool((response.get("result") or {}).get("isError"))


def classify_validation(label, response):
    text = content_text(response).lower()
    if label == "validate_kql_good":
        if result_is_error(response):
            return "FAIL", "known-good static validation returned an MCP error"
        return "PASS", "known-good query did not error"
    if label in ("validate_kql_semicolon", "validate_kql_source_operator"):
        blocked = any(word in text for word in ("invalid", "reject", "error", "semicolon", "source-introducing", "union"))
        if blocked or result_is_error(response):
            return "PASS", "unsafe query was rejected or reported invalid"
        return "FAIL", "unsafe query did not appear to be rejected"
    return "", ""


def smoke_score(label, response):
    specific_status, specific_reason = classify_validation(label, response)
    if specific_status:
        return specific_status, specific_reason
    if "error" in response:
        return "FAIL", str(response["error"])[:300]
    text = content_text(response)
    if any(marker.lower() in text.lower() for marker in SENSITIVE_MARKERS):
        return "FAIL", "result contains a raw sensitive marker"
    if result_is_error(response):
        return "PARTIAL", text[:300] or "tool returned isError"
    if not text.strip():
        return "PARTIAL", "empty tool result"
    return "PASS", text.replace("\n", " ")[:300]


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-command", nargs="+", default=DEFAULT_SERVER)
    ap.add_argument("--since", default="", help="Override since on smoke calls that accept it")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--allow-missing-claude-tools", action="store_true")
    return ap.parse_args()


def main():
    ns = parse_args()
    env = os.environ.copy()
    client = McpClient(ns.server_command, env=env, timeout=ns.timeout)
    started = time.time()
    report = {
        "schema": "berserk-mcp-live-smoke/v1",
        "server_command": ns.server_command,
        "role_env": env.get("BERSERK_MCP_ROLE", ""),
        "profile_env": env.get("BZRK_PROFILE", ""),
        "started_at_epoch": started,
        "tool_surface": {},
        "checks": [],
    }
    exit_code = 0
    try:
        init = client.initialize()
        report["initialize_ok"] = "error" not in init
        client.notify("notifications/initialized")
        tools = client.list_tools()
        names = {t.get("name") for t in tools}
        missing = sorted(EXPECTED_CLAUDE_TOOLS - names)
        report["tool_surface"] = {
            "count": len(tools),
            "missing_expected_claude_tools": missing,
        }
        if missing and not ns.allow_missing_claude_tools:
            exit_code = 1
        for label, name, args in READ_ONLY_CALLS:
            if name not in names:
                report["checks"].append({
                    "label": label,
                    "tool": name,
                    "status": "SKIP",
                    "reason": "tool not visible",
                })
                continue
            call_args = dict(args)
            if ns.since and "since" in call_args:
                call_args["since"] = ns.since
            t0 = time.time()
            try:
                response = client.call_tool(name, call_args)
                elapsed = time.time() - t0
                status, reason = smoke_score(label, response)
            except Exception as exc:
                elapsed = time.time() - t0
                response = {"exception": type(exc).__name__, "message": str(exc)}
                status, reason = "FAIL", str(exc)[:300]
            if status == "FAIL":
                exit_code = 1
            report["checks"].append({
                "label": label,
                "tool": name,
                "arguments": call_args,
                "status": status,
                "reason": reason,
                "elapsed_seconds": round(elapsed, 3),
                "is_error": result_is_error(response) if isinstance(response, dict) else True,
            })
    finally:
        client.close()

    print("# berserk-mcp live smoke")
    print("role:", report["role_env"] or "(unset)")
    print("profile:", report["profile_env"] or "(unset)")
    print("tools:", report["tool_surface"].get("count", 0))
    missing = report["tool_surface"].get("missing_expected_claude_tools") or []
    if missing:
        print("missing expected Claude tools:", ", ".join(missing))
    for check in report["checks"]:
        print(f"{check['status']:7} {check['label']}: {check['reason']}")
    if ns.json_out:
        Path(ns.json_out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("wrote:", ns.json_out)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

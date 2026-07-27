#!/usr/bin/env python3
"""AI FinOps data-quality eval for berserk-mcp.

This read-only eval calls the Claude/FinOps MCP tools and scores whether the
cluster contains enough telemetry to make the reports useful. A low score is
usually an ingestion/configuration gap, not automatically a code defect.

Examples:
  BERSERK_MCP_ROLE=claude python evals/ai_finops_data_quality.py
  python evals/ai_finops_data_quality.py --json-out /tmp/finops-dq.json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = [sys.executable, str(ROOT / "berserk_mcp.py")]

CALLS = [
    ("token_burn", "claude_token_burn", {"since": "30d ago"}),
    ("cost_day", "claude_cost_report", {"since": "30d ago", "group_by": "day"}),
    ("cost_model", "claude_cost_report", {"since": "30d ago", "group_by": "model"}),
    ("cost_project", "claude_cost_report", {"since": "30d ago", "group_by": "project"}),
    ("spend_day", "claude_spend_overview", {"since": "30d ago", "group_by": "day"}),
    ("spend_project", "claude_spend_overview", {"since": "30d ago", "group_by": "project"}),
    ("spend_model", "claude_spend_overview", {"since": "30d ago", "group_by": "model"}),
    ("efficiency", "claude_efficiency_insights", {"since": "30d ago"}),
    ("recommendations", "claude_harness_recommendations", {"since": "30d ago"}),
    ("management", "claude_management_report", {"scope": "portfolio", "since": "30d ago"}),
]

SIGNALS = {
    "exact_tokens": ("exact", "tokens_input", "input_tokens", "tokens output", "output_tokens"),
    "estimated_tokens": ("estimated", "estimate"),
    "message_dedup": ("message_id", "dedup", "billable call", "content-block"),
    "pricing": ("pricing", "priced", "unpriced", "catalog"),
    "project_attribution": ("project", "repository", "repo", "file target", "unattributed"),
    "feature_metadata": ("feature", "work item", "planned", "actual hours", "budget"),
    "agent_harness": ("agent", "harness", "profile", "harness_version"),
    "cache": ("cache", "cache_read", "cache_creation"),
    "data_quality": ("coverage", "missing", "insufficient", "freshness", "quality"),
    "pseudonym_privacy": ("pseudonym", "hmac", "owner", "personal data"),
}

FAIL_MARKERS = (
    "traceback",
    "internal error",
    "semaphore released too many times",
    "raw secret",
)


class McpClient:
    def __init__(self, command, env=None):
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

    def notify(self, method):
        self.send({"jsonrpc": "2.0", "method": method})

    def initialize(self):
        return self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "berserk-mcp-finops-dq", "version": "1"},
            },
        )

    def list_tools(self):
        return self.request("tools/list").get("result", {}).get("tools", [])

    def call_tool(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})


def content_text(response):
    parts = (response.get("result") or {}).get("content") or []
    return "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text")


def is_error(response):
    return "error" in response or bool((response.get("result") or {}).get("isError"))


def score_signal(text, terms):
    lower = text.lower()
    return any(term in lower for term in terms)


def extract_numbers(text):
    return [float(m.group(0).replace(",", "")) for m in re.finditer(r"\b\d+(?:,\d{3})*(?:\.\d+)?\b", text)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-command", nargs="+", default=DEFAULT_SERVER)
    ap.add_argument("--since", default="30d ago")
    ap.add_argument("--json-out", default="")
    ns = ap.parse_args()

    env = os.environ.copy()
    client = McpClient(ns.server_command, env=env)
    report = {
        "schema": "berserk-mcp-ai-finops-data-quality/v1",
        "server_command": ns.server_command,
        "role_env": env.get("BERSERK_MCP_ROLE", ""),
        "profile_env": env.get("BZRK_PROFILE", ""),
        "started_at_epoch": time.time(),
        "calls": [],
        "signals": {name: False for name in SIGNALS},
        "score": 0,
        "max_score": len(SIGNALS),
        "verdict": "unknown",
    }
    exit_code = 0
    combined = []
    try:
        init = client.initialize()
        if "error" in init:
            raise RuntimeError(str(init["error"]))
        client.notify("notifications/initialized")
        names = {t.get("name") for t in client.list_tools()}
        for label, tool, args in CALLS:
            if tool not in names:
                report["calls"].append({"label": label, "tool": tool, "status": "SKIP", "reason": "tool not visible"})
                continue
            call_args = dict(args)
            if "since" in call_args:
                call_args["since"] = ns.since
            t0 = time.time()
            try:
                response = client.call_tool(tool, call_args)
                text = content_text(response)
                combined.append(text)
                status = "FAIL" if is_error(response) else "PASS"
                lower = text.lower()
                if any(marker in lower for marker in FAIL_MARKERS):
                    status = "FAIL"
                if status == "FAIL":
                    exit_code = 1
                nums = extract_numbers(text)
                reason = (text.replace("\n", " ")[:300] if text.strip() else "empty result")
                report["calls"].append({
                    "label": label,
                    "tool": tool,
                    "arguments": call_args,
                    "status": status,
                    "reason": reason,
                    "elapsed_seconds": round(time.time() - t0, 3),
                    "numeric_values_seen": len(nums),
                })
            except Exception as exc:
                exit_code = 1
                report["calls"].append({
                    "label": label,
                    "tool": tool,
                    "arguments": call_args,
                    "status": "FAIL",
                    "reason": str(exc)[:300],
                    "elapsed_seconds": round(time.time() - t0, 3),
                })
    finally:
        client.close()

    all_text = "\n".join(combined)
    for name, terms in SIGNALS.items():
        report["signals"][name] = score_signal(all_text, terms)
    report["score"] = sum(1 for present in report["signals"].values() if present)
    ratio = report["score"] / float(report["max_score"] or 1)
    if exit_code:
        report["verdict"] = "fail"
    elif ratio >= 0.75:
        report["verdict"] = "good"
    elif ratio >= 0.45:
        report["verdict"] = "partial"
    else:
        report["verdict"] = "poor"

    print("# AI FinOps data-quality eval")
    print("role:", report["role_env"] or "(unset)")
    print("profile:", report["profile_env"] or "(unset)")
    print("verdict:", report["verdict"])
    print("signal score:", f"{report['score']}/{report['max_score']}")
    for name, present in report["signals"].items():
        print(("PASS " if present else "MISS ") + name)
    print()
    for call in report["calls"]:
        print(f"{call['status']:5} {call['label']}: {call['reason']}")
    if ns.json_out:
        Path(ns.json_out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("wrote:", ns.json_out)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

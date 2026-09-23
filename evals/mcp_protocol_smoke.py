#!/usr/bin/env python3
"""Offline protocol smoke for berserk-mcp.

This starts a local berserk-mcp process and verifies the installed MCP surface
without requiring live Berserk authentication:

- legacy stdio initialize remains `2025-06-18`;
- Codex-style tools/list progress metadata is accepted;
- gated `2026-07-28` stdio discovery works;
- modern tools/list exposes private cache hints and output schemas;
- modern tools/call returns `resultType`;
- broad expensive search returns `input_required` before running `bzrk`;
- task creation/get works when the client advertises task capability;
- optional HTTP transport can serve healthz and JSON-RPC on loopback.

The script is intentionally read-only from Berserk's perspective. It uses static
validation and preflight behavior where possible. The task check uses
`claude_management_report`; without live auth, the task may complete with a
redacted/auth-failure result, but task lifecycle plumbing must still work.

Examples:
  python3 evals/mcp_protocol_smoke.py
  python3 evals/mcp_protocol_smoke.py --server-command berserk-mcp
  python3 evals/mcp_protocol_smoke.py --include-http
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
import contextlib

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = [sys.executable, str(ROOT / "berserk_mcp.py")]
LEGACY_VERSION = "2025-06-18"
MODERN_VERSION = "2026-07-28"
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"


class StdioClient:
    def __init__(self, command, env):
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
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)

    def request(self, method, params=None):
        req_id = self.next_id
        self.next_id += 1
        obj = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            obj["params"] = params
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read()[:2000]
            raise RuntimeError(f"MCP server exited before response: {stderr}")
        return json.loads(line)

    def request_collecting(self, method, params=None):
        """Send a request; return (response, notifications written before it)."""
        req_id = self.next_id
        self.next_id += 1
        obj = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            obj["params"] = params
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        notifications = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                stderr = self.proc.stderr.read()[:2000]
                raise RuntimeError(f"MCP server exited before response: {stderr}")
            msg = json.loads(line)
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                return msg, notifications
            notifications.append(msg)

    def notify(self, method, params=None):
        obj = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            obj["params"] = params
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()


def modern_meta():
    return {
        META_PROTOCOL_VERSION: MODERN_VERSION,
        META_CLIENT_INFO: {"name": "berserk-mcp-protocol-smoke", "version": "1"},
        META_CLIENT_CAPABILITIES: {"tasks": {}},
    }


def check(report, label, ok, detail=""):
    report["checks"].append(
        {
            "label": label,
            "status": "PASS" if ok else "FAIL",
            "detail": detail,
        }
    )
    if not ok:
        report["failed"] = True


def run_stdio_smoke(command):
    env = os.environ.copy()
    env["BERSERK_MCP_ENABLE_2026_07_28"] = "1"
    env.setdefault("BERSERK_MCP_ROLE", "claude")
    # This smoke test exercises protocol plumbing (does `search`, tools/call,
    # etc. work end-to-end), not tiering/routing policy -- role=claude alone
    # would now resolve to tier=small (issue #4) and hide `search`, breaking
    # a plumbing test that has nothing to do with tiering. Force deep so the
    # full protocol surface stays exercised regardless of tier defaults.
    env.setdefault("BERSERK_MCP_TIER", "deep")
    env.setdefault(
        "BERSERK_MCP_REPORT_DIR",
        str(Path(tempfile.gettempdir()) / "berserk-mcp-protocol-smoke-reports"),
    )
    # Pre-seed one saved query so the saved__<name> projection (issue #5)
    # has something to project, without needing a real save_query call
    # (which would run a live, authenticated verification query -- this
    # script is deliberately offline). A private, unpredictable path --
    # not a fixed name in the shared temp dir, which another local user or
    # process could pre-create as a symlink to a victim-writable file and
    # have write_text() follow it.
    learned_fd, learned_name = tempfile.mkstemp(prefix="berserk-mcp-protocol-smoke-learned-", suffix=".json")
    learned_path = Path(learned_name)
    with os.fdopen(learned_fd, "w") as f:
        json.dump(
            [{"name": "smoke_probe", "description": "protocol smoke probe", "kql": "default | take 1"}],
            f,
        )
    env["BERSERK_MCP_LEARNED_PATH"] = str(learned_path)
    client = StdioClient(command, env)
    report = {"transport": "stdio", "checks": [], "failed": False}
    meta = modern_meta()
    try:
        init = client.request(
            "initialize",
            {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "berserk-mcp-protocol-smoke", "version": "1"},
            },
        )
        check(
            report,
            "legacy initialize stays stable",
            init.get("result", {}).get("protocolVersion") == LEGACY_VERSION,
            json.dumps(init)[:300],
        )
        client.notify("notifications/initialized")

        codex_tools = client.request(
            "tools/list",
            {"_meta": {"progressToken": 0}},
        )
        check(
            report,
            "Codex tools/list progress metadata",
            "error" not in codex_tools and bool(codex_tools.get("result", {}).get("tools")),
            json.dumps(codex_tools)[:300],
        )

        discover = client.request("server/discover", {"_meta": meta})
        d_result = discover.get("result", {})
        check(
            report,
            "modern server/discover",
            d_result.get("resultType") == "complete"
            and d_result.get("supportedVersions") == [MODERN_VERSION, LEGACY_VERSION]
            and d_result.get("cacheScope") == "private",
            json.dumps(discover)[:300],
        )

        tools_response = client.request("tools/list", {"_meta": meta})
        tools_result = tools_response.get("result", {})
        tools = {t.get("name"): t for t in tools_result.get("tools", [])}
        check(
            report,
            "modern tools/list cache hints",
            tools_result.get("cacheScope") == "private" and tools_result.get("ttlMs") == 300000,
            json.dumps(tools_result)[:300],
        )
        check(
            report,
            "modern outputSchema for reporting tools",
            "outputSchema" in tools.get("claude_management_report", {}),
            "claude_management_report metadata",
        )
        check(
            report,
            "saved query projected into tools/list",
            "saved__smoke_probe" in tools,
            json.dumps(sorted(n for n in tools if n.startswith("saved__")))[:300],
        )

        saved_call = client.request(
            "tools/call",
            {
                "_meta": meta,
                "name": "saved__smoke_probe",
                "arguments": {},
            },
        )
        saved_result = saved_call.get("result", {})
        saved_text = "".join(c.get("text", "") for c in saved_result.get("content", []) if isinstance(c, dict))
        check(
            report,
            "saved query directly callable",
            # A missing "result" (top-level JSON-RPC error, e.g. an
            # uncaught exception in the saved__ dispatch branch) must fail
            # this check -- the substring test alone treats an empty/absent
            # result the same as a real successful reply, since "unknown
            # tool" is trivially absent from "".
            "error" not in saved_call and "result" in saved_call and "unknown tool" not in saved_text.lower(),
            json.dumps(saved_call)[:300],
        )

        call = client.request(
            "tools/call",
            {
                "_meta": meta,
                "name": "validate_kql",
                "arguments": {"kql": "default | take 1", "mode": "static"},
            },
        )
        check(
            report,
            "modern tools/call resultType",
            call.get("result", {}).get("resultType") == "complete" and call.get("result", {}).get("isError") is False,
            json.dumps(call)[:300],
        )

        listen_id = client.next_id
        # No response while the subscription lives; the next line is the acknowledgement.
        ack = client.request("subscriptions/listen", {"_meta": meta, "notifications": {"toolsListChanged": True}})
        check(
            report,
            "modern subscriptions/listen acknowledged with subscription id",
            ack.get("method") == "notifications/subscriptions/acknowledged"
            and ack.get("params", {}).get("_meta", {}).get("io.modelcontextprotocol/subscriptionId") == listen_id
            and ack.get("params", {}).get("notifications") == {"toolsListChanged": True},
            json.dumps(ack)[:300],
        )

        broad = client.request(
            "tools/call",
            {
                "_meta": meta,
                "name": "search",
                "arguments": {
                    "kql": "default | where message contains 'timeout'",
                    "since": "7d ago",
                },
            },
        )
        check(
            report,
            "modern expensive search preflight returns input_required",
            broad.get("result", {}).get("resultType") == "input_required",
            json.dumps(broad)[:300],
        )

        task = client.request(
            "tools/call",
            {
                "_meta": meta,
                "name": "claude_generate_dashboard",
                "arguments": {
                    "dashboard": "portfolio",
                    "since": "1h ago",
                    "format": "markdown",
                    "filename": "protocol-smoke-dashboard.md",
                    "as_task": True,
                },
            },
        )
        task_obj = task.get("result", {}).get("task", {})
        task_id = task_obj.get("id")
        check(
            report,
            "modern task create",
            isinstance(task_id, str) and task_id.startswith("task_"),
            json.dumps(task)[:300],
        )
        if task_id:
            got = client.request("tasks/get", {"_meta": meta, "taskId": task_id})
            got_task = got.get("result", {}).get("task", {})
            check(
                report,
                "modern tasks/get",
                got_task.get("id") == task_id
                and got_task.get("status")
                in {
                    "pending",
                    "running",
                    "complete",
                    "failed",
                    "cancelled",
                },
                json.dumps(got)[:300],
            )
    finally:
        client.close()
        learned_path.unlink(missing_ok=True)
    return report


_BZRK_STUB = """#!/usr/bin/env python3
import json, sys
if "--version" in sys.argv:
    print("bzrk 0.0.0-protocol-smoke-stub")
else:
    print(json.dumps({"Tables": [{"schema": {"columns": [{"name": "n"}]}, "rows": [[1]]}]}))
"""


def run_list_changed_smoke(command):
    """A real save must reach a modern client as notifications/tools/list_changed
    on its subscriptions/listen stream, stamped with the subscription id.

    save_query verifies its query with bzrk before persisting, so this run
    points BZRK_BIN at a stub and the saved-query store at a private temp dir:
    offline, and no real store or Berserk is touched.
    """
    report = {"transport": "stdio (list_changed delivery)", "checks": [], "failed": False}
    if os.name == "nt":
        # A .cmd stub would route arguments through cmd.exe; not worth the risk
        # for a smoke check. Linux CI covers this path.
        report["checks"].append(
            {"label": "modern save delivers tools/list_changed on listen stream", "status": "SKIP", "detail": ""}
        )
        return report
    tmp = tempfile.mkdtemp(prefix="berserk-mcp-list-changed-")
    client = None
    meta = modern_meta()
    try:
        stub = Path(tmp) / "bzrk"
        stub.write_text(_BZRK_STUB, encoding="utf-8")
        stub.chmod(0o700)
        env = os.environ.copy()
        env.update(
            {
                "BERSERK_MCP_ENABLE_2026_07_28": "1",
                "BERSERK_MCP_TIER": "deep",
                "BERSERK_MCP_ROLE": "all",
                "BZRK_BIN": str(stub),
                "BERSERK_MCP_LEARNED_PATH": str(Path(tmp) / "learned.json"),
                "BERSERK_MCP_REPORT_DIR": str(Path(tmp) / "reports"),
            }
        )
        client = StdioClient(command, env)
        listen_id = client.next_id
        ack = client.request("subscriptions/listen", {"_meta": meta, "notifications": {"toolsListChanged": True}})
        check(
            report,
            "listen acknowledged before save",
            ack.get("method") == "notifications/subscriptions/acknowledged",
            json.dumps(ack)[:300],
        )
        resp, notes = client.request_collecting(
            "tools/call",
            {
                "_meta": meta,
                "name": "save_query",
                "arguments": {"name": "smoke_saved", "description": "list_changed smoke", "kql": "default | take 1"},
            },
        )
        saved = "result" in resp and resp["result"].get("isError") is False
        check(report, "save_query succeeds against stub bzrk", saved, json.dumps(resp)[:300])
        changed = [n for n in notes if n.get("method") == "notifications/tools/list_changed"]
        check(
            report,
            "modern save delivers tools/list_changed on listen stream",
            len(changed) == 1
            and changed[0].get("params", {}).get("_meta", {}).get("io.modelcontextprotocol/subscriptionId")
            == listen_id,
            json.dumps(notes)[:300],
        )
        listed = client.request("tools/list", {"_meta": meta})
        names = {t.get("name") for t in listed.get("result", {}).get("tools", [])}
        check(report, "saved query now in tools/list", "saved__smoke_saved" in names, "")
    except Exception as exc:
        check(report, "list_changed smoke completed", False, f"{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            client.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return report


def free_loopback_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def http_post(url, body, token):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Host": "127.0.0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_http_smoke(command):
    port = free_loopback_port()
    token = "protocol-smoke-token"
    env = os.environ.copy()
    env["BERSERK_MCP_ENABLE_2026_07_28"] = "1"
    env["BERSERK_MCP_HTTP_ENABLE"] = "1"
    env["BERSERK_MCP_HTTP_BIND"] = f"127.0.0.1:{port}"
    env["BERSERK_MCP_HTTP_AUTH_TOKEN"] = token
    env.setdefault("BERSERK_MCP_ROLE", "claude")
    # This smoke test exercises protocol plumbing (does `search`, tools/call,
    # etc. work end-to-end), not tiering/routing policy -- role=claude alone
    # would now resolve to tier=small (issue #4) and hide `search`, breaking
    # a plumbing test that has nothing to do with tiering. Force deep so the
    # full protocol surface stays exercised regardless of tier defaults.
    env.setdefault("BERSERK_MCP_TIER", "deep")
    http_command = list(command)
    http_command.append("--http")
    proc = subprocess.Popen(
        http_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    report = {"transport": "http", "checks": [], "failed": False}
    base = f"http://127.0.0.1:{port}"
    try:
        ready = False
        for _ in range(50):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=1) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    ready = resp.status == 200 and body.get("status") == "ok"
                if ready:
                    break
            except Exception:
                time.sleep(0.1)
        check(report, "http /healthz", ready)
        if ready:
            init = http_post(
                base + "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LEGACY_VERSION,
                        "capabilities": {},
                        "clientInfo": {
                            "name": "berserk-mcp-protocol-smoke",
                            "version": "1",
                        },
                    },
                },
                token,
            )
            check(
                report,
                "http JSON-RPC initialize",
                init.get("result", {}).get("protocolVersion") == LEGACY_VERSION,
                json.dumps(init)[:300],
            )
            tools = http_post(
                base + "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {"_meta": modern_meta()},
                },
                token,
            )
            check(
                report,
                "http modern tools/list",
                tools.get("result", {}).get("cacheScope") == "private",
                json.dumps(tools)[:300],
            )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
    return report


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-command", nargs="+", default=DEFAULT_SERVER)
    ap.add_argument("--include-http", action="store_true")
    ap.add_argument("--json-out", default="")
    return ap.parse_args()


def main():
    ns = parse_args()
    reports = [run_stdio_smoke(ns.server_command), run_list_changed_smoke(ns.server_command)]
    if ns.include_http:
        reports.append(run_http_smoke(ns.server_command))
    failed = any(report["failed"] for report in reports)
    print("# berserk-mcp protocol smoke")
    for report in reports:
        print("transport:", report["transport"])
        for item in report["checks"]:
            print(f"{item['status']:4} {item['label']}")
            if item["status"] == "FAIL" and item.get("detail"):
                print("     " + item["detail"])
    if ns.json_out:
        Path(ns.json_out).write_text(
            json.dumps({"schema": "berserk-mcp-protocol-smoke/v1", "reports": reports}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        print("wrote:", ns.json_out)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

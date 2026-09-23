#!/usr/bin/env python3
"""Optional check: MCP Inspector's tool-schema portability lint, both protocol eras.

Runs a pinned @modelcontextprotocol/inspector (Node, via npx) against this
server's tools/list in the legacy and 2026-07-28 eras. Not part of required CI:
the project is pure-stdlib Python and this needs Node >= 22.19. Run with
`make inspector-check`.

Fail closed, as scripts/mcp_scan_gate.py does. A pass needs a parseable report
listing at least the server's static tools and no schema findings. `--strict`
alone exits non-zero only for error-severity findings, so warnings are read
from the JSON instead. The modern run must show outputSchema on some tool,
which proves the 2026-07-28 era was actually negotiated.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

INSPECTOR_VERSION = "2.7.0"
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "berserk_mcp.py"
ERAS = ("legacy", "modern")


def evaluate(era, returncode, stdout, min_tools):
    """Return a list of problems for one era's run; empty means pass."""
    if returncode not in (0, 6):
        return [f"inspector exited {returncode}"]
    try:
        report = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return ["inspector output is not JSON"]
    if not isinstance(report, dict):
        return ["inspector output is not a JSON object"]
    tools = (report.get("result") or {}).get("tools")
    if not isinstance(tools, list) or len(tools) < min_tools:
        count = len(tools) if isinstance(tools, list) else 0
        return [f"expected at least {min_tools} tools, got {count}"]
    problems = []
    if returncode == 6:
        problems.append("inspector reported error-severity schema findings")
    findings = report.get("schemaFindings")
    if findings:
        problems.append("schema findings: " + json.dumps(findings)[:2000])
    if era == "modern" and not any(isinstance(t, dict) and "outputSchema" in t for t in tools):
        problems.append("no tool has outputSchema; the 2026-07-28 era was not negotiated")
    return problems


def _static_tool_count():
    sys.path.insert(0, str(REPO_ROOT))
    import berserk_mcp

    return len(berserk_mcp.TOOLS) + len(berserk_mcp.MGMT_TOOLS)


def _run(era, tmp):
    cmd = [
        "npx",
        "--yes",
        f"@modelcontextprotocol/inspector@{INSPECTOR_VERSION}",
        "--cli",
        # The target must come before the options: with a leading `--` the CLI
        # reports "Method is required".
        sys.executable,
        str(SERVER),
        "--method",
        "tools/list",
        "--protocol-era",
        era,
        "--format",
        "json",
        "--strict",
        "--client-config",
        str(Path(tmp) / "client.json"),
        "-e",
        "BERSERK_MCP_ENABLE_2026_07_28=1",
    ]
    env = dict(os.environ, MCP_CATALOG_PATH=str(Path(tmp) / "catalog.json"), npm_config_ignore_scripts="true")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=tmp, env=env)


def main():
    min_tools = _static_tool_count()
    failed = False
    with tempfile.TemporaryDirectory(prefix="inspector-check-") as tmp:
        for era in ERAS:
            try:
                proc = _run(era, tmp)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"FAIL {era}: could not run inspector ({type(exc).__name__}: {exc})")
                failed = True
                continue
            problems = evaluate(era, proc.returncode, proc.stdout, min_tools)
            if problems:
                failed = True
                print(f"FAIL {era}:")
                for problem in problems:
                    print(f"  - {problem}")
                stderr = "\n".join(line for line in proc.stderr.splitlines() if not line.startswith("[berserk-mcp]"))
                if stderr.strip():
                    print("  inspector stderr:\n" + stderr[:4000])
            else:
                print(f"PASS {era}: no schema findings")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

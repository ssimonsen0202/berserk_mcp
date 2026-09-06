#!/usr/bin/env python3
"""CI gate for the Cisco MCP scanner (cisco-ai-mcp-scanner).

Runs the scanner's YARA analyzer against this server's live tools/list and
fails the build on any unsafe tool that is not in the committed baseline.

Three design decisions, each with a reason:

**1. YARA only -- no API/LLM/Behavioral/VirusTotal analyzers.**
Those transmit tool definitions or source to a third party and need API
keys. CI must not depend on an external service or ship this code
anywhere. YARA is fully local and needs no credentials.

**2. Fail closed, explicitly.**
The scanner itself fails open in at least two places, observed 2026-09-06:
`vulnerable-package` reported "SAFE (0 findings)" while its own log showed
`pip-audit exited with code 2 and produced no JSON output`, and the LLM
analyzer counted three tools as SAFE after they errored with "Empty
response from LLM". A security gate that reports pass when it did not run
is worse than no gate. So this script treats *all* of these as failures:
non-zero exit, unparseable output, zero tools scanned, and any tool whose
record is not `status == "completed"`.

**3. Baseline of accepted findings, rather than a zero-findings gate.**
The YARA rules flag imperative routing and limitation language in tool
descriptions as "prompt injection" -- e.g. `top_cpu`'s "use ONLY when...
use host_cpu instead". That phrasing is deliberate: it is the tool
disambiguation this project measured as improving real routing accuracy
(mistral-saba 86.3%->92.2%, deepseek-v4-flash 90.2%->94.1%; see
docs/model-routing-cost-validation-2026-08-23.md). A zero-findings gate
would create standing pressure to delete the thing that demonstrably
works. Known findings are therefore accepted by name in
scripts/mcp_scan_baseline.json, with a reason recorded per entry, and the
gate fails only on findings that are *new*.

Usage:
    python3 scripts/mcp_scan_gate.py [--update-baseline]

Exit codes: 0 clean, 1 new findings or a scan that did not complete.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "mcp_scan_baseline.json"
SERVER = REPO_ROOT / "berserk_mcp.py"


def run_scanner(stderr_path):
    """Run the scanner and return its parsed JSON records.

    Raises RuntimeError on anything that means "the scan did not actually
    happen" -- never returns an empty/partial result that a caller could
    mistake for a clean run.
    """
    cmd = [
        "mcp-scanner", "--analyzers", "yara", "--raw",
        "stdio", "--stdio-command", sys.executable,
        "--stdio-arg", str(SERVER),
        "--stderr-file", str(stderr_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    except FileNotFoundError as exc:
        # An absent scanner must read as "this check did not run", not as a
        # pass -- the same fail-closed rule the rest of this file applies.
        raise RuntimeError(
            "mcp-scanner is not installed or not on PATH. Install it with: "
            "uv tool install --python 3.13 cisco-ai-mcp-scanner "
            "(or pip install cisco-ai-mcp-scanner)") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"mcp-scanner exited {proc.returncode}\n{proc.stderr[-2000:]}")
    try:
        records = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"mcp-scanner produced unparseable output: {exc}\n"
            f"first 500 bytes: {proc.stdout[:500]!r}") from exc
    if not isinstance(records, list) or not records:
        raise RuntimeError(
            "mcp-scanner returned no tool records -- treating as a failed "
            "scan, not a clean one")
    incomplete = [r.get("tool_name", "<unnamed>") for r in records
                  if r.get("status") != "completed"]
    if incomplete:
        raise RuntimeError(
            "these tools did not complete analysis, so their result is "
            f"unknown rather than safe: {', '.join(sorted(incomplete))}")
    return records


def unsafe_tools(records):
    """{tool_name: "SEVERITY: threat summary"} for every non-safe record."""
    out = {}
    for rec in records:
        if rec.get("is_safe"):
            continue
        name = rec.get("tool_name", "<unnamed>")
        bits = []
        for analyzer, finding in (rec.get("findings") or {}).items():
            if not isinstance(finding, dict):
                continue
            sev = finding.get("severity", "?")
            summary = finding.get("threat_summary", "")
            if sev != "SAFE":
                bits.append(f"{analyzer}={sev}: {summary}")
        out[name] = "; ".join(bits) or "unsafe, no analyzer detail"
    return out


def load_baseline():
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8")).get("accepted", {})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update-baseline", action="store_true",
                    help="rewrite the baseline from the current scan; review "
                         "the diff and add a reason for each new entry before "
                         "committing it")
    args = ap.parse_args()

    stderr_path = REPO_ROOT / ".mcp-scan-stderr.log"
    try:
        records = run_scanner(stderr_path)
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        stderr_path.unlink(missing_ok=True)

    found = unsafe_tools(records)
    baseline = load_baseline()

    if args.update_baseline:
        payload = {
            "_comment": "Accepted MCP-scanner findings. Each entry needs a "
                        "reason. A finding belongs here only if it is a "
                        "false positive or an accepted risk -- never to "
                        "silence something real.",
            "accepted": {
                name: baseline.get(name, {"reason": "TODO: explain why this "
                                                    "is accepted",
                                          "detail": detail})
                for name, detail in sorted(found.items())
            },
        }
        BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"baseline rewritten with {len(found)} entries -- add a reason "
              f"for each before committing")
        return 0

    new = sorted(set(found) - set(baseline))
    stale = sorted(set(baseline) - set(found))

    print(f"scanned {len(records)} tools; "
          f"{len(found)} unsafe, {len(baseline)} accepted in baseline")

    if stale:
        # Not a failure: a finding disappearing is good news. But it means
        # the baseline is carrying an entry that no longer applies, and a
        # baseline nobody prunes stops being reviewed.
        print("\nNOTE: baseline entries no longer reported (safe to remove):")
        for name in stale:
            print(f"  - {name}")

    if new:
        print("\nFAIL: new unsafe tools not in the baseline:", file=sys.stderr)
        for name in new:
            print(f"  - {name}: {found[name]}", file=sys.stderr)
        print("\nIf these are genuine, fix them. If they are false positives, "
              "run with --update-baseline and record a reason per entry.",
              file=sys.stderr)
        return 1

    print("\nOK: no new findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())

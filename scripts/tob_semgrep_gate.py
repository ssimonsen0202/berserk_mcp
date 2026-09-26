#!/usr/bin/env python3
"""CI gate for the Trail of Bits semgrep rules (github.com/trailofbits/semgrep-rules).

Runs the ruleset's `generic/` rules and its one standard-library Python rule
over the whole repository and fails on any finding.

Four design decisions, each with a reason:

**1. Only the rules that can match here.**
The generic rules (curl/wget TLS and plaintext URLs, SSH host-key checks,
openssl/gpg/tar flags, database/Redis/AMQP plaintext) match text in any file,
so they check the example commands in README, docs and configs against the
"plaintext only on loopback" policy in SECURITY.md. Of the Python rules, only
`tarfile-extractall-traversal` targets the standard library; the other 22
target numpy, pandas, PyTorch, TensorFlow and similar libraries, which this
stdlib-only project never imports. Evaluated 2026-09-26: zero findings on
main, all 41 rules firing on the ruleset's own fixtures.

**2. Fetched at a pinned commit, not vendored.**
The rules are AGPL-3.0 and this repository is MIT, so they are cloned at run
time rather than copied into .semgrep/. The clone is checked out at
PINNED_COMMIT and the checked-out HEAD is verified, so an upstream change
cannot alter what CI enforces without a reviewed bump here.

**3. Fail closed.**
A scanner that prints zero findings may not have run. The gate fails on:
a clone or checkout failure, a HEAD other than the pinned commit, a rules
checkout with modified or extra files, semgrep exiting with an error,
unparseable output, any semgrep "errors" entry, zero scanned files, and any
tracked file (other than this gate) missing from semgrep's scanned list. The
repository's ignore-nothing .semgrepignore keeps tests/ in scope; a binary or
oversized file that semgrep skips fails the gate and must be excluded here
deliberately.

**4. Canary before the real scan.**
The same rules first scan a small planted example written the way this
repo's docs write commands (inside a fenced Markdown block, plus an
unfiltered tarfile.extractall). Every expected rule must fire, or the gate
fails: a rule that silently stopped matching (a semgrep upgrade, a rule
rewrite) would otherwise turn into a permanent pass.

Usage: python scripts/tob_semgrep_gate.py [--rules-dir DIR]
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_URL = "https://github.com/trailofbits/semgrep-rules.git"
PINNED_COMMIT = "31390b3a99c04c81522d1b37c8d1900aa2dd4094"
RULE_PATHS = ("generic", "python/tarfile-extractall-traversal.yaml")
SELF_EXCLUDE = "scripts/tob_semgrep_gate.py"
_REGULAR_FILE_MODES = {"100644", "100755"}

CANARY_FILES = {
    "docs/setup.md": (
        "# Setup\n\n```bash\n"
        "curl -k https://collector.example/v1/logs\n"
        "curl -sS http://collector.example:4318/v1/logs\n"
        "ssh -o StrictHostKeyChecking=no admin@host.example\n"
        "wget --no-check-certificate https://files.example/a\n"
        "```\n"
    ),
    "extract.py": "import tarfile\nwith tarfile.open(path) as t:\n    t.extractall(dest)\n",
}
CANARY_EXPECTED = {
    "curl-insecure",
    "curl-unencrypted-url",
    "ssh-disable-host-key-checking",
    "wget-no-check-certificate",
    "tarfile-extractall-traversal",
}


def fetch_rules(rules_dir):
    """Clone the ruleset at PINNED_COMMIT into rules_dir (reusing an existing
    clone) and verify HEAD. Returns an error string or None."""
    rules_dir = Path(rules_dir)
    if not (rules_dir / ".git").is_dir():
        result = subprocess.run(["git", "clone", "--quiet", REPO_URL, str(rules_dir)], capture_output=True, text=True)
        if result.returncode != 0:
            return f"git clone failed: {result.stderr.strip()}"
    result = subprocess.run(
        ["git", "-C", str(rules_dir), "checkout", "--quiet", PINNED_COMMIT], capture_output=True, text=True
    )
    if result.returncode != 0:
        return f"git checkout {PINNED_COMMIT} failed: {result.stderr.strip()}"
    head = subprocess.run(["git", "-C", str(rules_dir), "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.stdout.strip() != PINNED_COMMIT:
        return f"rules HEAD is {head.stdout.strip()!r}, expected {PINNED_COMMIT}"
    # HEAD alone does not pin the files: a reused clone could hold edited or
    # extra rule files at the right commit.
    status = subprocess.run(
        ["git", "-C", str(rules_dir), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
    )
    if status.returncode != 0 or status.stdout.strip():
        return f"rules checkout is not clean: {status.stdout.strip()[:300] or status.stderr.strip()}"
    return None


def unscanned_tracked_files(target, scanned):
    """Tracked regular files in `target` (except this gate) that semgrep did
    not scan, or None when git cannot list them.

    Symlinks (mode 120000) and submodule gitlinks (160000) are left out:
    semgrep never scans them, and their content lives elsewhere. -z gives
    unquoted paths; errors="replace" makes a non-UTF-8 name mismatch (and so
    fail with its name) instead of raising.
    """
    result = subprocess.run(
        ["git", "-C", str(target), "ls-files", "-z", "--stage"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        return None
    tracked = set()
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        if meta.split(" ", 1)[0] in _REGULAR_FILE_MODES:
            tracked.add(path)
    return sorted(tracked - {SELF_EXCLUDE} - set(scanned))


def run_semgrep(rules_dir, target, exclude=()):
    """Run the selected rules over target. Returns (report_dict, error)."""
    argv = ["semgrep", "--metrics=off", "--quiet", "--json"]
    for rule_path in RULE_PATHS:
        argv += ["--config", str(Path(rules_dir) / rule_path)]
    for pattern in exclude:
        argv += ["--exclude", pattern]
    argv.append(str(target))
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        return None, f"semgrep exited {result.returncode}: {result.stderr.strip()[:500]}"
    try:
        return json.loads(result.stdout), None
    except ValueError:
        return None, "semgrep output was not JSON"


def evaluate(report):
    """Check a semgrep JSON report. Returns (findings, error): findings as
    'rule path:line' strings, error when the scan cannot be trusted."""
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        return [], "semgrep report has no results list"
    if report.get("errors"):
        return [], f"semgrep reported {len(report['errors'])} error(s): {json.dumps(report['errors'])[:500]}"
    if not report.get("paths", {}).get("scanned"):
        return [], "semgrep scanned zero files"
    findings = [f"{r['check_id'].rsplit('.', 1)[-1]} {r['path']}:{r['start']['line']}" for r in report["results"]]
    return findings, None


def fired_rules(findings):
    return {finding.split(" ", 1)[0] for finding in findings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rules-dir", default=str(Path(tempfile.gettempdir()) / "tob-semgrep-rules"))
    parser.add_argument("--target", default=".")
    args = parser.parse_args(argv)

    error = fetch_rules(args.rules_dir)
    if error:
        print(f"FAIL: {error}")
        return 1

    with tempfile.TemporaryDirectory() as canary:
        for name, content in CANARY_FILES.items():
            path = Path(canary) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        report, error = run_semgrep(args.rules_dir, canary)
        findings, error = evaluate(report) if not error else ([], error)
        missing = CANARY_EXPECTED - fired_rules(findings)
        if error or missing:
            print(f"FAIL: canary check: {error or 'rules did not fire: ' + ', '.join(sorted(missing))}")
            return 1
    print(f"canary OK: {len(CANARY_EXPECTED)} expected rules fired")

    # This file holds the canary's planted commands, so it would match itself.
    report, error = run_semgrep(args.rules_dir, args.target, exclude=(SELF_EXCLUDE,))
    findings, error = evaluate(report) if not error else ([], error)
    if error:
        print(f"FAIL: {error}")
        return 1
    missing = unscanned_tracked_files(args.target, report["paths"]["scanned"])
    if missing is None or missing:
        detail = "git ls-files failed" if missing is None else ", ".join(missing[:20])
        print(f"FAIL: tracked files were not scanned: {detail}")
        return 1
    scanned = len(report["paths"]["scanned"])
    if findings:
        print(f"FAIL: {len(findings)} finding(s) in {scanned} scanned files:")
        for finding in findings:
            print(f"  {finding}")
        return 1
    print(f"OK: 0 findings in {scanned} scanned files (trailofbits/semgrep-rules@{PINNED_COMMIT[:7]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

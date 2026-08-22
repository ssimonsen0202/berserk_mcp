#!/usr/bin/env python3
"""CI gate for the router eval harness (issue #13, Milestone 1's cheap
half). Runs run_eval.py --backend mock against router_cases.jsonl and
fails the build if tool-selection accuracy drops below the published
threshold.

Kept as a separate script from run_eval.py deliberately: run_eval.py is
the harness itself (actively growing -- two-tier routing, real-backend
support), and the CI-critical pass/fail decision should not be entangled
with that. This script only reads run_eval.py's saved JSON results file;
it never imports or modifies run_eval.py.

Threshold history: 65% set 2026-08-20 against the router_cases.jsonl
suite as it stood then (31 cases, mock backend measured 87.1%). Ratchet
up as evals/router_cases.jsonl grows more targeted phrasings (issue #13's
own Phase 2, tracked as the same issue).
"""
import json
import math
import subprocess
import sys
from pathlib import Path

MIN_TOOL_ACCURACY = 0.65

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = REPO_ROOT / "evals" / "router_cases.jsonl"
RESULTS_DIR = REPO_ROOT / "evals" / "results"


def check_accuracy(results, min_accuracy=MIN_TOOL_ACCURACY):
    """Pure decision logic: (ok, message). Fails closed on anything that
    isn't a plain numeric tool_accuracy field -- a malformed results
    payload (e.g. run_eval.py changing its output schema) must not
    silently pass a CI gate that exists specifically to catch regressions."""
    accuracy = results.get("tool_accuracy")
    if not isinstance(accuracy, (int, float)) or isinstance(accuracy, bool):
        return False, f"tool_accuracy missing or non-numeric in results: {accuracy!r}"
    if not math.isfinite(accuracy) or not (0 <= accuracy <= 1):
        return False, f"tool_accuracy out of the expected [0, 1] fraction range: {accuracy!r}"
    pct = accuracy * 100
    min_pct = min_accuracy * 100
    if accuracy < min_accuracy:
        return False, f"router eval regression: tool-selection accuracy {pct:.1f}% is below the {min_pct:.0f}% CI threshold"
    return True, f"router eval OK: {pct:.1f}% >= {min_pct:.0f}% threshold"


def _run_eval_and_load_results():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    before = set(RESULTS_DIR.glob("mock_*.json"))
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "evals" / "run_eval.py"),
         "--backend", "mock", str(CASES_PATH)],
        capture_output=True, text=True, timeout=120,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        sys.exit(f"run_eval.py exited {result.returncode}")
    after = set(RESULTS_DIR.glob("mock_*.json"))
    new_files = after - before
    if not new_files:
        sys.exit("run_eval.py did not produce a new results file")
    results_path = max(new_files, key=lambda p: p.stat().st_mtime)
    return json.loads(results_path.read_text(encoding="utf-8"))


def main():
    results = _run_eval_and_load_results()
    ok, message = check_accuracy(results)
    print(message)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()

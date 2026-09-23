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
own Phase 2, tracked as the same issue). Raised to 75% on 2026-09-23: the
suite had grown to 54 cases and the mock measured 79.6% (43/54), so 65%
allowed about eight more misses before failing; 75% allows about two.
"""

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

MIN_TOOL_ACCURACY = 0.75

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = REPO_ROOT / "evals" / "router_cases.jsonl"


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
        return (
            False,
            f"router eval regression: tool-selection accuracy {pct:.1f}% is below the {min_pct:.0f}% CI threshold",
        )
    return True, f"router eval OK: {pct:.1f}% >= {min_pct:.0f}% threshold"


def _run_eval_and_load_results(run=subprocess.run):
    """Run the mock eval into a private temp file and load exactly that file.

    Snapshot-diffing the shared evals/results/ dir raced any other mock run:
    run_eval.py stamps filenames to the second, so a run landing in the same
    second produced an already-present name and the gate saw "no new file"
    (or could read another run's report). A fresh temp dir cannot collide,
    and the gate still fails closed if run_eval.py writes nothing there."""
    with tempfile.TemporaryDirectory(prefix="ci_gate-") as tmp:
        results_path = Path(tmp) / "results.json"
        result = run(
            [
                sys.executable,
                str(REPO_ROOT / "evals" / "run_eval.py"),
                "--backend",
                "mock",
                "--out",
                str(results_path),
                str(CASES_PATH),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            sys.exit(f"run_eval.py exited {result.returncode}")
        if not results_path.is_file():
            sys.exit(f"run_eval.py did not produce a results file at {results_path}")
        return json.loads(results_path.read_text(encoding="utf-8"))


def main():
    results = _run_eval_and_load_results()
    ok, message = check_accuracy(results)
    print(message)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()

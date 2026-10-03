"""Working notes stay out of the public repository.

Handoffs, briefs, plans, raw evidence and one-off results belong in the
maintainer's private notes, not in git. A few older notes remain tracked
because the README, a spec, a release note or code cites them; they are
listed in KEPT. Anything else that matches NOTE_PATTERNS fails this test.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NOTE_PATTERNS = (
    r"handoff",
    r"brief",
    r"raw-evidence",
    r"live-test-results",
    r"implementation-plan",
    r"-plan(-\d{4}-\d{2}-\d{2})?\.md$",
    r"^docs/superpowers/",
    r"^evals/results-",
    r"^evals/results/mock_",
    r"(^|/)progress\.md$",
    r"(?-i:(^|/)CLAUDE\.md$)",
    r"^docs/[^/]*-\d{4}-\d{2}-\d{2}\.md$",
    r"SESSION-HANDOFF",
)

# Cited by README, a spec, a release note, code or a test; kept so those
# links and reads keep working. Do not add to this list: put new notes in
# the private notes folder instead.
KEPT = {
    "docs/berserk-dev-brief-2026-08-20.md",
    "docs/codex-backtest-handoff-2026-09-03.md",
    "docs/handoff-five-proposed-prs-2026-08-16.md",
    "docs/investor-brief-berserk-mcp-2026-08-23.md",
    "docs/mcp-guidance-and-repository-review-brief-2026-09-25.md",
    "docs/mistral-small-optimization-plan-2026-09-03.md",
    "docs/task-brief-collision-clusters-2026-09-03.md",
    "evals/model-eval-plan.md",
    "docs/mcp-guidance-review-2026-09-26.md",
    "docs/model-routing-cost-validation-2026-08-23.md",
    "evals/results-2026-06-22.md",
    "evals/results/mock_mock-20260723-113523.json",
}


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return out.splitlines()


class NoWorkingNotesTest(unittest.TestCase):
    def test_git_lists_files(self):
        self.assertGreater(len(tracked_files()), 100)  # fail closed on an empty listing

    def test_no_new_working_notes_are_tracked(self):
        pattern = re.compile("|".join(NOTE_PATTERNS), re.IGNORECASE)
        notes = sorted(f for f in tracked_files() if pattern.search(f) and f not in KEPT)
        self.assertEqual(notes, [], "move these to the private notes folder and git rm --cached them")

    def test_kept_notes_still_exist(self):
        # A kept note that was deleted should leave the list too.
        tracked = set(tracked_files())
        self.assertEqual(sorted(KEPT - tracked), [])


if __name__ == "__main__":
    unittest.main()

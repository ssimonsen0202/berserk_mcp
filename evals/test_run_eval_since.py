"""expect_since_valid scoring: a `since` the server would reject fails the case."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_eval  # noqa: E402

CASE = {"expect_tool": "list_hosts", "expect_since_any": ["2h", "2 h", "120"], "expect_since_valid": True}


class SinceValidityScoringTest(unittest.TestCase):
    def test_valid_since_passes(self):
        for since in ("2h ago", "2 hours ago", "120m ago", "last 2 hours"):
            with self.subTest(since=since):
                self.assertEqual(run_eval.score_case(CASE, "list_hosts", {"since": since}), (True, True))

    def test_server_rejected_since_fails_even_when_the_substring_matches(self):
        # "1.2h" contains "2h" but the server accepts only whole numbers.
        for since in ("1.2h ago", "2 hrss ago", "2h ago; x"):
            with self.subTest(since=since):
                self.assertEqual(run_eval.score_case(CASE, "list_hosts", {"since": since}), (True, False))

    def test_missing_since_fails(self):
        self.assertEqual(run_eval.score_case(CASE, "list_hosts", {}), (True, False))

    def test_cases_without_the_flag_score_as_before(self):
        case = {"expect_tool": "list_hosts", "expect_since_any": ["2h"]}
        self.assertEqual(run_eval.score_case(case, "list_hosts", {"since": "1.2h ago"}), (True, True))


if __name__ == "__main__":
    unittest.main()

"""The advertised `since` pattern: complete for the runtime, and small.

Review 2026-09-26 (docs/mcp-guidance-review-2026-09-26.md), P2: the
letter-by-letter case-insensitive pattern was ~370 bytes repeated in every
tool, about 30% of each lane's tools/list.
"""

import itertools
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import berserk_mcp as bm  # noqa: E402


def _ecma(pattern):
    # JSON Schema patterns are ECMA-262, where \d and \s are ASCII-only;
    # Python's re is Unicode-aware unless told otherwise.
    return re.compile(pattern, re.ASCII)


class SinceSchemaTest(unittest.TestCase):
    def test_schema_accepts_every_form_the_runtime_accepts(self):
        # A grammar-constrained client must never be stopped from sending a
        # value the server would accept.
        pattern = _ecma(bm._SINCE_SCHEMA_PATTERN)
        cases = [str.lower, str.upper, str.title]
        units = list(bm._SINCE_HOURS_FACTORS)
        for unit, case, gap, ago in itertools.product(units, cases, ("", " "), ("", " ago", " AGO")):
            value = f"3{gap}{case(unit)}{ago}"
            with self.subTest(value=value):
                if bm.valid_since(value):
                    self.assertRegex(value, pattern)
        for value in ("now", "NOW", "Now"):
            self.assertRegex(value, pattern)

    def test_schema_rejects_free_text(self):
        pattern = _ecma(bm._SINCE_SCHEMA_PATTERN)
        for value in ("yesterday", "last week", "1h ago; drop", "-5m", "", "5 minutesss ago"):
            with self.subTest(value=value):
                self.assertIsNone(pattern.match(value))

    def test_unknown_unit_passing_the_schema_is_rejected_by_the_server(self):
        self.assertRegex("5 xyz", _ecma(bm._SINCE_SCHEMA_PATTERN))
        self.assertFalse(bm.valid_since("5 xyz"))
        saved = bm.run_bzrk
        try:
            bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: ("rows", False)
            text, is_err = bm.handle_call("list_hosts", {"since": "5 xyz"})
        finally:
            bm.run_bzrk = saved
        self.assertTrue(is_err)
        self.assertIn("invalid 'since' value", text)

    def test_pattern_stays_small(self):
        self.assertLessEqual(len(json.dumps(bm._SINCE_SCHEMA_PATTERN)), 64)


if __name__ == "__main__":
    unittest.main()

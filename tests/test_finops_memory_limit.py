"""claude_spend_overview silently undercounted multi-day windows (found
2026-10-02). Over more than about a day, the usage query's first summarize
reaches Berserk's 10 MB SummarizeMemoryLimit. Berserk then drops groups and
says so only in the response's `warnings`, which ai_finops ignored. For
2026-09-28, a 5-day window counted 142 of 302 API calls.

_fetch_usage now reads the warnings. On a limit warning it halves the window
and queries each half, down to _MIN_USAGE_SLICE. If even that slice hits the
limit, it fails closed instead of returning a partial total.
"""

import json
import re
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_finops as af  # noqa: E402
import berserk_mcp as bm  # noqa: E402

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
_FILTER_RE = re.compile(r"timestamp >= datetime\(([^)]+)\)(?: and timestamp < datetime\(([^)]+)\))?")


def _dt(text):
    return datetime.fromisoformat(text)


class _FakeBerserk:
    """Answers the usage query like Berserk: one event per hour in the
    queried slice, plus a SummarizeMemoryLimit warning when the slice is
    wider than `limit_hours`."""

    def __init__(self, limit_hours):
        self.limit_hours = limit_hours
        self.slices = []

    def __call__(self, kql, since):
        match = _FILTER_RE.search(kql)
        if match is None:  # no window filter: the whole retention period
            start, end = NOW - timedelta(days=365), NOW
        else:
            start = _dt(match.group(1))
            end = _dt(match.group(2)) if match.group(2) else NOW
        self.slices.append((start, end))
        hours = (end - start).total_seconds() / 3600
        doc = {
            "Tables": [
                {"schema": {"columns": [{"name": "day"}, {"name": "events"}]}, "rows": [[start.isoformat(), hours]]}
            ],
            "warnings": [],
        }
        if hours > self.limit_hours:
            doc["warnings"].append(
                {"kind": "SummarizeMemoryLimit", "message": "Summarize memory limit reached (10.0 MB)."}
            )
        return json.dumps(doc), False


class FetchUsageMemoryLimitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.fake = _FakeBerserk(limit_hours=24)
        af.configure(
            search=self.fake,
            table="default",
            business_store_path=root / "business.json",
            decision_store_path=root / "decisions.json",
            pseudonym_key_path=root / "pseudonym.key",
            report_dir=root / "reports",
            since_hours=lambda since: float(re.match(r"(\d+)", since).group(1)) * 24,
        )
        self._now = mock.patch.object(af, "_utcnow", lambda: NOW)
        self._now.start()

    def tearDown(self):
        self._now.stop()
        af.configure(
            search=bm.bzrk_search_json,
            table=bm.TABLE,
            catalog_path=bm.FINOPS_PRICING_CATALOG_PATH,
            business_store_path=bm.FINOPS_BUSINESS_STORE_PATH,
            decision_store_path=bm.FINOPS_DECISION_STORE_PATH,
            pseudonym_key_path=bm.FINOPS_PSEUDONYM_KEY_PATH,
            report_dir=bm.FINOPS_REPORT_DIR,
            otlp_endpoint=bm.FINOPS_OTLP_ENDPOINT,
            since_hours=bm.finops_since_hours,
        )
        self._tmp.cleanup()

    def test_window_within_the_limit_is_one_query(self):
        rows, error = af._fetch_usage("1d ago")
        self.assertFalse(error, rows)
        self.assertEqual(len(self.fake.slices), 1)
        self.assertEqual(sum(r["events"] for r in rows), 24)

    def test_wide_window_is_split_until_no_slice_hits_the_limit(self):
        rows, error = af._fetch_usage("5d ago")
        self.assertFalse(error, rows)
        # Every event of the 120-hour window is counted exactly once.
        self.assertEqual(sum(r["events"] for r in rows), 120)
        answered = sorted(s for s in self.fake.slices if (s[1] - s[0]) <= timedelta(hours=24))
        self.assertEqual(answered[0][0], NOW - timedelta(hours=120))
        self.assertEqual(answered[-1][1], NOW)
        for (_, end), (start, _) in zip(answered, answered[1:], strict=False):
            self.assertEqual(end, start, "slices must touch: no gap and no overlap")

    def test_limit_at_the_smallest_slice_fails_closed(self):
        self.fake.limit_hours = 0  # every slice hits the limit
        text, error = af._fetch_usage("1d ago")
        self.assertTrue(error)
        self.assertIn("memory limit", text)
        self.assertIn("partial", text)

    def test_limit_without_a_parseable_window_fails_closed(self):
        af.configure(search=self.fake, table="default", since_hours=lambda since: None)
        self.fake.limit_hours = 0
        text, error = af._fetch_usage("now")
        self.assertTrue(error)
        self.assertIn("memory limit", text)

    def test_any_limit_kind_counts_and_other_warnings_do_not(self):
        self.assertEqual(
            af._limit_warnings(json.dumps({"warnings": [{"kind": "SummarizeMemoryLimit"}]})), ["SummarizeMemoryLimit"]
        )
        self.assertEqual(
            af._limit_warnings(json.dumps({"warnings": [{"kind": "RowLimitReached"}]})), ["RowLimitReached"]
        )
        self.assertEqual(af._limit_warnings(json.dumps({"warnings": [{"kind": "SlowQuery"}]})), [])
        self.assertEqual(af._limit_warnings("(no rows)"), [])

    def test_usage_query_takes_an_absolute_window(self):
        start, end = NOW - timedelta(hours=6), NOW
        query = af.usage_aggregate_query(start, end)
        self.assertIn("timestamp >= datetime(2026-10-02T06:00:00Z)", query)
        self.assertIn("timestamp < datetime(2026-10-02T12:00:00Z)", query)
        self.assertNotIn("timestamp >=", af.usage_aggregate_query())

    def test_context_tokens_count_one_hour_writes_once(self):
        # cache_creation_1h_tokens is a subset of cache_creation_tokens, so
        # the long-context size must not add it a second time.
        query = af.usage_aggregate_query()
        expr = re.search(r"extend context_tokens=(.*?) \| extend organization", query).group(1)
        self.assertIn("cache_create", expr)
        self.assertNotIn("cache_create_1h", expr)


if __name__ == "__main__":
    unittest.main()

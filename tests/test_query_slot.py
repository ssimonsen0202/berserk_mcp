"""Every bzrk query launch holds a query slot (Codex Security finding 1).

BERSERK_MCP_MAX_CONCURRENT_QUERIES bounds how many bzrk query processes run
at once. It was enforced only in bzrk_search, so the public `schema` tool,
the stale-schema refresh and the self_check doctor queries called run_bzrk
directly and could exceed the limit. run_bzrk now takes a slot for every
`search` launch unless the calling thread already holds one.

These tests use the real run_bzrk with a fake subprocess runner, because
most tests replace bm.run_bzrk and would bypass the enforcement entirely.
"""

import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402

_COUNT_JSON = json.dumps({"Tables": [{"schema": {"columns": [{"name": "Count"}]}, "rows": [[5]]}]})


class RecordingSemaphore:
    """A BoundedSemaphore that records how many slots are held."""

    def __init__(self, n):
        self._sem = threading.BoundedSemaphore(n)
        self._lock = threading.Lock()
        self.held = 0
        self.acquires = 0
        self.max_held = 0

    def acquire(self, timeout=None):
        ok = self._sem.acquire(timeout=timeout)
        if ok:
            with self._lock:
                self.held += 1
                self.acquires += 1
                self.max_held = max(self.max_held, self.held)
        return ok

    def release(self):
        with self._lock:
            self.held -= 1
        self._sem.release()


class QuerySlotTest(unittest.TestCase):
    def setUp(self):
        self.sem = RecordingSemaphore(2)
        self.launches = []  # (is_search, slots held at launch)

        def fake_runner(argv, timeout, *args, **kwargs):
            is_search = "search" in argv
            self.launches.append((is_search, self.sem.held))
            if "--version" in argv:
                stdout = b"bzrk 2026-08-19.test"
            elif "--json" in argv:
                stdout = _COUNT_JSON.encode()
            else:
                stdout = b"service  count\ncheckout 3"
            return {
                "returncode": 0,
                "stdout": stdout,
                "stderr": b"",
                "stdout_overflow": False,
                "stderr_overflow": False,
                "stderr_watch_matched": False,
                "streams_complete": True,
            }

        patches = [
            mock.patch.object(bm, "_QUERY_SEMAPHORE", self.sem),
            mock.patch.object(bm, "_run_argv_bounded", fake_runner),
            mock.patch.object(bm, "_RESOLVED_BZRK_BIN", "/fake/bzrk"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def assert_every_search_held_a_slot(self):
        searches = [held for is_search, held in self.launches if is_search]
        self.assertTrue(searches, "no search was launched")
        self.assertTrue(all(held >= 1 for held in searches), f"slots held at each search launch: {searches}")
        self.assertEqual(self.sem.held, 0, "every slot must be released")

    def test_schema_tool_holds_a_slot(self):
        bm.do_schema()
        self.assert_every_search_held_a_slot()

    def test_schema_refresh_holds_a_slot(self):
        bm._schema_fetcher()
        self.assert_every_search_held_a_slot()

    def test_doctor_queries_hold_a_slot(self):
        for check in (bm._doctor_check_auth, bm._doctor_check_table_reachable, bm._doctor_check_recent_rows):
            with self.subTest(check=check.__name__):
                self.launches.clear()
                check()
                self.assert_every_search_held_a_slot()

    def test_search_holds_exactly_one_slot(self):
        # bzrk_search takes the slot itself; run_bzrk must not take a second.
        bm.bzrk_search("default | take 1", "1h ago")
        self.assert_every_search_held_a_slot()
        self.assertEqual(self.sem.acquires, 1)

    def test_live_validation_holds_exactly_one_slot(self):
        # The diagnostics path takes the slot itself, then calls run_bzrk.
        with mock.patch.object(bm, "KQL_LIVE_VALIDATION", True):
            text, is_err = bm._handle_validate_kql({"kql": "default | take 1", "mode": "live", "use_schema": False})
        self.assertFalse(is_err, text)
        self.assert_every_search_held_a_slot()
        self.assertEqual(self.sem.acquires, 1)

    def test_version_does_not_take_a_slot(self):
        bm.run_bzrk(["--version"])
        self.assertEqual(self.sem.acquires, 0)
        self.assertEqual(self.launches, [(False, 0)])

    def test_full_queue_refuses_the_launch(self):
        one = RecordingSemaphore(1)
        with mock.patch.object(bm, "_QUERY_SEMAPHORE", one):
            self.assertTrue(one.acquire(timeout=0))  # another caller holds the only slot
            try:
                text, is_err = bm.run_bzrk(["-P", "p", "search", "default | take 1"], timeout=0.05)
            finally:
                one.release()
        self.assertTrue(is_err)
        self.assertIn("query queue is full", text)
        self.assertEqual(self.launches, [], "no process may start without a slot")

    def test_slot_is_released_when_the_runner_raises(self):
        def boom(*args, **kwargs):
            raise RuntimeError("runner failed")

        with mock.patch.object(bm, "_run_argv_bounded", boom):
            text, is_err = bm.run_bzrk(["-P", "p", "search", "default | take 1"])
        self.assertTrue(is_err)
        self.assertEqual(self.sem.held, 0)
        self.assertEqual(self.sem.acquires, 1)

    def test_no_limit_still_runs(self):
        with mock.patch.object(bm, "_QUERY_SEMAPHORE", None):
            text, is_err = bm.run_bzrk(["-P", "p", "search", "default | take 1"])
        self.assertFalse(is_err, text)


if __name__ == "__main__":
    unittest.main()

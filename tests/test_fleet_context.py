"""Each tool call keeps its own query budget under concurrency (Codex finding 2).

handle_call used to store the tool name and budget in one process-global
variable and restore the previous value on exit. When two calls overlapped,
the call that finished first could restore a stale value (or nothing) while
the other was still running. That call's bzrk_search then read no budget and
fell back to the 120 s default instead of its own budget. The context is now
per-call (a ContextVar), so each thread sees only its own.
"""

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402

BASE_BUDGET = 7.0
LONG_DEFAULT = 600


class FleetContextTest(unittest.TestCase):
    def setUp(self):
        bm._reset_fleet_state()
        self.addCleanup(bm._reset_fleet_state)
        self.timeouts = {}  # call label -> timeout bzrk_search passed to run_bzrk
        self.events = {name: threading.Event() for name in ("first_in", "second_in", "first_done")}

        def fake_run_bzrk(args, timeout="default"):
            self.timeouts[threading.current_thread().name] = timeout
            return "(no rows)", False

        patches = [
            mock.patch.object(bm, "run_bzrk", fake_run_bzrk),
            mock.patch.object(bm, "TOOL_BUDGET_SECONDS", BASE_BUDGET),
            mock.patch.object(bm, "BUDGET_PER_HOUR_SECONDS", 0.0),
            mock.patch.object(bm, "DEFAULT_TIMEOUT", LONG_DEFAULT),
            mock.patch.object(bm, "CACHE_TTL_SECONDS", 0.0),
            mock.patch.object(bm, "FAIL_COOLDOWN_SECONDS", 0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run_overlapping(self, first, second, finish_order):
        """Start `first`, then `second` while `first` is inside its call.

        finish_order "first" or "second" picks which call returns first. The
        call that is still running queries only after the other has returned.
        """
        ev = self.events

        def fake_uncached(name, arguments):
            label = threading.current_thread().name
            if label == "first":
                ev["first_in"].set()
                ev["second_in"].wait(5)
                if finish_order == "second":
                    ev["first_done"].wait(5)  # wait until the second call has returned
                bm.bzrk_search("default | take 1", "1h ago")
            else:
                ev["first_in"].wait(5)
                ev["second_in"].set()
                if finish_order == "first":
                    ev["first_done"].wait(5)
                bm.bzrk_search("default | take 1", "1h ago")
            return "ok", False

        threads = {}

        def call(label, tool):
            bm.handle_call(tool, {})
            if label != finish_order:
                return
            ev["first_done"].set()

        with mock.patch.object(bm, "_handle_call_uncached", fake_uncached):
            for label, tool in (("first", first), ("second", second)):
                threads[label] = threading.Thread(target=call, args=(label, tool), name=label)
            threads["first"].start()
            ev["first_in"].wait(5)
            threads["second"].start()
            for t in threads.values():
                t.join(10)
                self.assertFalse(t.is_alive(), "a call did not finish")

    def _expected(self, tool):
        return BASE_BUDGET * max(1.0, bm._tool_budget_multiplier(tool))

    def test_each_call_keeps_its_budget_when_the_other_finishes_first(self):
        for finish_order in ("second", "first"):
            with self.subTest(finish_order=finish_order):
                self.timeouts.clear()
                for ev in self.events.values():
                    ev.clear()
                self._run_overlapping("search", "list_hosts", finish_order)
                self.assertEqual(self.timeouts.get("first"), self._expected("search"))
                self.assertEqual(self.timeouts.get("second"), self._expected("list_hosts"))

    def test_no_budget_is_left_behind_after_the_calls(self):
        self._run_overlapping("search", "list_hosts", "second")
        # A query outside any tool call runs with no per-tool budget.
        bm.bzrk_search("default | take 1", "1h ago")
        self.assertEqual(self.timeouts.get("MainThread"), "default")


if __name__ == "__main__":
    unittest.main()

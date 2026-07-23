import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm


class Phase3ToolsTest(unittest.TestCase):
    def setUp(self):
        self.original_run = bm.run_bzrk
        self.calls = []
        bm.CACHE_TTL_SECONDS = 0
        bm.FAIL_COOLDOWN_SECONDS = 0

        def fake_run(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append((list(args), timeout))
            return "rows", False

        bm.run_bzrk = fake_run
        bm._reset_fleet_state()

    def tearDown(self):
        bm.run_bzrk = self.original_run
        bm._reset_fleet_state()

    def test_native_query_shapes(self):
        self.assertIn("make-series", bm.q_detect_anomalies())
        self.assertIn("series_decompose_anomalies", bm.q_detect_anomalies())
        self.assertIn("make-series", bm.q_forecast_capacity("system.memory.usage"))
        self.assertIn("series_fit_line", bm.q_forecast_capacity("system.memory.usage"))
        self.assertIn("similarto", bm.q_find_similar("database timeout"))

    def test_tools_are_registered_with_lane_roles(self):
        tools = {tool["name"]: tool for tool in bm.TOOLS}
        self.assertEqual(tools["detect_anomalies"]["roles"], ["sre", "soc"])
        self.assertEqual(tools["forecast_capacity"]["roles"], ["sre"])
        self.assertEqual(tools["find_similar"]["roles"], ["sre", "soc"])

    def test_input_guards_do_not_call_backend(self):
        for name, args in (
            ("detect_anomalies", {"service": "bad service!"}),
            ("forecast_capacity", {"metric": "user.secret"}),
            ("find_similar", {"description": "timeout' | take 1"}),
            ("find_similar", {"description": "x" * 501}),
        ):
            with self.subTest(name=name):
                text, is_err = bm.handle_call(name, args)
                self.assertTrue(is_err)
                self.assertEqual(self.calls, [])

    def test_similarity_parser_error_is_graceful(self):
        def parser_error(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append((list(args), timeout))
            return "PARSE ERROR at similarto", True

        bm.run_bzrk = parser_error
        text, is_err = bm.handle_call("find_similar", {"description": "database timeout"})
        self.assertFalse(is_err)
        self.assertIn("Semantic indexing is not enabled", text)

    def test_forecast_refuses_weak_or_downward_trends(self):
        payload = {
            "Tables": [{
                "schema": {"columns": [
                    {"name": "host"}, {"name": "fit"},
                ]},
                "rows": [["node-a", [0.42, -1.0, 0, 0, 0, []]]],
            }]
        }

        def fit(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append((list(args), timeout))
            return json.dumps(payload), False

        bm.run_bzrk = fit
        text, is_err = bm.handle_call(
            "forecast_capacity", {"metric": "system.memory.usage"}
        )
        self.assertFalse(is_err)
        self.assertIn("no reliable trend", text)
        self.assertIn("R²=0.420", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

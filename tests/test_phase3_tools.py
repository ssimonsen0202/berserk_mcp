import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm


class Phase3ToolsTest(unittest.TestCase):
    def setUp(self):
        self.original_run = bm.run_bzrk
        self.orig_cache_ttl = bm.CACHE_TTL_SECONDS
        self.orig_fail_cooldown = bm.FAIL_COOLDOWN_SECONDS
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
        bm.CACHE_TTL_SECONDS = self.orig_cache_ttl
        bm.FAIL_COOLDOWN_SECONDS = self.orig_fail_cooldown
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

    def test_control_characters_and_overlong_interpolated_inputs_are_rejected(self):
        cases = (
            ("find_similar", {"description": "timeout\nretry"}),
            ("claude_search", {"term": "timeout\t"}),
            ("claude_search", {"term": "x" * 501}),
            ("trace_analyze", {"trace_id": "a" * 65}),
            ("logs_for_service", {"service": "s" * 129}),
            ("detect_anomalies", {"service": "s" * 129}),
            ("forecast_capacity", {
                "metric": "system.memory.usage", "host": "h" * 129,
            }),
            ("request_discovery", {"service": "s" * 129}),
            ("generate_parser", {"service": "s" * 129}),
        )
        for name, args in cases:
            with self.subTest(name=name):
                self.calls.clear()
                _, is_err = bm.handle_call(name, args)
                self.assertTrue(is_err)
                self.assertEqual(self.calls, [])

    def test_interpolated_tool_schemas_publish_length_bounds(self):
        tools = {tool["name"]: tool for tool in bm.TOOLS}
        self.assertEqual(
            tools["claude_search"]["inputSchema"]["properties"]["term"]["maxLength"],
            bm.MAX_SEARCH_TERM_CHARS,
        )
        self.assertEqual(
            tools["trace_analyze"]["inputSchema"]["properties"]["trace_id"]["maxLength"],
            bm.MAX_TRACE_ID_CHARS,
        )
        self.assertEqual(
            tools["logs_for_service"]["inputSchema"]["properties"]["service"]["maxLength"],
            bm.MAX_INTERPOLATED_NAME_CHARS,
        )

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

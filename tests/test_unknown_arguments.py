"""tools/call rejects argument names the tool's schema does not declare.

Review 2026-09-26 (docs/mcp-guidance-review-2026-09-26.md), P2: a misspelled
optional filter (`svc` for `service`) was ignored, so the call ran unfiltered
and the answer looked filtered.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import berserk_mcp as bm  # noqa: E402


def _call(name, arguments, meta=None):
    params = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    response = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    result = response["result"]
    return result["content"][0]["text"], result.get("isError", False)


class UnknownArgumentTest(unittest.TestCase):
    def setUp(self):
        self._saved = (bm.run_bzrk, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED, bm.CACHE_TTL_SECONDS, bm.load_learned)
        self.calls = []

        def fake(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return "service  n\nnginx  1", False

        bm.run_bzrk = fake
        bm.CACHE_TTL_SECONDS = 0
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "all", bm.TIER_DEEP

    def tearDown(self):
        bm.run_bzrk, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED, bm.CACHE_TTL_SECONDS, bm.load_learned = self._saved

    # Covers SECURITY.md#trust-boundaries
    def test_misspelled_optional_filter_is_rejected_before_bzrk_runs(self):
        text, is_err = _call("detect_anomalies", {"svc": "nginx"})
        self.assertTrue(is_err)
        self.assertEqual(text, "unknown argument 'svc' for detect_anomalies; valid: service, since")
        self.assertEqual(self.calls, [])

    def test_argument_a_tool_does_not_have_is_rejected(self):
        text, is_err = _call("host_cpu", {"host": "web-01"})
        self.assertTrue(is_err)
        self.assertIn("unknown argument 'host' for host_cpu; valid: since", text)
        self.assertEqual(self.calls, [])

    def test_declared_arguments_still_work(self):
        _, is_err = _call("detect_anomalies", {"service": "nginx", "since": "1h ago"})
        self.assertFalse(is_err)
        self.assertTrue(self.calls)

    def test_protocol_arguments_are_accepted(self):
        for extra in ({"as_task": False}, {"allow_expensive": True}):
            with self.subTest(extra=extra):
                self.calls.clear()
                _, is_err = _call("list_hosts", dict(extra))
                self.assertFalse(is_err)
                self.assertTrue(self.calls)

    def test_hidden_tool_answers_unknown_tool_whatever_its_arguments(self):
        # The argument error lists valid names, so it must never reach a hidden tool.
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "ops", bm.TIER_SMALL
        text, is_err = _call("search", {"bogus": 1})
        self.assertTrue(is_err)
        self.assertEqual(text, "unknown tool: search")

    def test_unknown_tool_is_unchanged(self):
        text, is_err = _call("no_such_tool", {"bogus": 1})
        self.assertTrue(is_err)
        self.assertEqual(text, "unknown tool: no_such_tool")

    def test_saved_query_tools_check_their_schema(self):
        bm.load_learned = lambda: [{"name": "probe", "kql": bm.TABLE + " | take 1", "since": "1h ago"}]
        text, is_err = _call("saved__probe", {"servce": "x"})
        self.assertTrue(is_err)
        self.assertIn("unknown argument 'servce' for saved__probe", text)
        _, is_err = _call("saved__probe", {"since": "1h ago"})
        self.assertFalse(is_err)

    def test_echoed_names_are_bounded(self):
        arguments = {("k" * 200) + str(i): 1 for i in range(9)}
        text, is_err = _call("list_hosts", arguments)
        self.assertTrue(is_err)
        self.assertIn("and 4 more", text)
        self.assertNotIn("k" * 65, text)

    def test_every_tool_accepts_all_its_declared_arguments(self):
        # Guards against false rejections: nothing a schema declares is refused.
        for tool in bm.TOOLS + bm.MGMT_TOOLS:
            with self.subTest(tool=tool["name"]):
                arguments = {name: None for name in tool["inputSchema"].get("properties", {})}
                self.assertIsNone(bm._unknown_argument_error(tool, arguments))

    def test_modern_protocol_path_rejects_too(self):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {"tasks": {}},
        }
        enabled = bm.ENABLE_MCP_2026_07_28
        try:
            bm.ENABLE_MCP_2026_07_28 = True
            response = bm.dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "detect_anomalies", "arguments": {"svc": "nginx"}, "_meta": meta},
                }
            )
        finally:
            bm.ENABLE_MCP_2026_07_28 = enabled
        self.assertIn("unknown argument 'svc'", json.dumps(response))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

"""Tests for berserk_mcp. Pure stdlib (unittest); no live Berserk needed.

Strategy: monkeypatch `run_bzrk` to capture the exact argv that would be sent to
the bzrk CLI and return canned output. This verifies the full dispatch path —
generated KQL, default time windows, injection guards, JSON-RPC shape, and the
learning loop — without a real backend.
"""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402


class BerserkMcpTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return ("OK", False)

        self._orig = bm.run_bzrk
        bm.run_bzrk = fake_run_bzrk

        # Isolate the learned store in a temp file.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_learned = bm.LEARNED_PATH
        bm.LEARNED_PATH = Path(self._tmp.name) / "learned.json"

    def tearDown(self):
        bm.run_bzrk = self._orig
        bm.LEARNED_PATH = self._orig_learned
        self._tmp.cleanup()

    # ---- argv / KQL wiring ----
    def test_simple_tool_argv_and_default_since(self):
        text, err = bm.handle_call("list_containers", {})
        self.assertFalse(err)
        self.assertEqual(
            self.calls[-1],
            ["-P", bm.PROFILE, "search", bm.Q_CONTAINERS, "--since", "15m ago"],
        )

    def test_since_override(self):
        bm.handle_call("errors_by_service", {"since": "3d ago"})
        self.assertEqual(self.calls[-1][-1], "3d ago")
        # default would have been 1h ago
        bm.handle_call("errors_by_service", {})
        self.assertEqual(self.calls[-1][-1], "1h ago")

    def test_since_rejected(self):
        text, err = bm.handle_call("top_cpu", {"since": "garbage; rm -rf /"})
        self.assertTrue(err)
        self.assertIn("invalid 'since'", text)
        self.assertEqual(self.calls, [])  # must not have shelled out

    def test_since_various_valid(self):
        for s in ("now", "15m ago", "2 hours ago", "1d", "30 minutes ago", "3w ago"):
            self.calls.clear()
            text, err = bm.handle_call("top_cpu", {"since": s})
            self.assertFalse(err, s)
            self.assertEqual(self.calls[-1][-1], s)

    def test_locked_query_strings(self):
        """Guard the most-used KQL against accidental edits during refactors."""
        self.assertEqual(
            bm.Q_CONTAINERS,
            "default | where isnotnull(metric_name) | where isnotempty(resource['container.name']) "
            "| summarize samples=count() by container=tostring(resource['container.name']) "
            "| sort by container asc",
        )
        self.assertEqual(
            bm.Q_HOST_CPU,
            "default | where metric_name == 'system.cpu.load_average.1m' "
            "| summarize load_1m=avg(value) by host=tostring(resource['host.name']) "
            "| sort by load_1m desc",
        )

    def test_all_simple_tools_callable(self):
        for name in bm.SIMPLE:
            self.calls.clear()
            text, err = bm.handle_call(name, {})
            self.assertFalse(err, name)
            self.assertEqual(self.calls[-1][0:3], ["-P", bm.PROFILE, "search"], name)

    # ---- injection guards ----
    def test_logs_rejects_bad_service(self):
        text, err = bm.handle_call("logs_for_service", {"service": "a' or '1'='1"})
        self.assertTrue(err)
        self.assertIn("invalid service", text)
        self.assertEqual(self.calls, [])  # must not have shelled out

    def test_logs_accepts_good_service(self):
        text, err = bm.handle_call("logs_for_service", {"service": "postgres"})
        self.assertFalse(err)
        self.assertIn("resource['service.name'] == 'postgres'", self.calls[-1][3])

    def test_cc_search_rejects_quotes(self):
        for bad in ["a'b", 'a"b', "a|b", "a\\b", "a`b"]:
            self.calls.clear()
            text, err = bm.handle_call("claude_search", {"term": bad})
            self.assertTrue(err, bad)
            self.assertEqual(self.calls, [], bad)

    def test_cc_search_accepts_plain_term(self):
        text, err = bm.handle_call("claude_search", {"term": "TimeoutError"})
        self.assertFalse(err)
        self.assertIn("contains 'TimeoutError'", self.calls[-1][3])

    def test_missing_required_args(self):
        for name in ("logs_for_service", "search", "claude_search"):
            text, err = bm.handle_call(name, {})
            self.assertTrue(err, name)

    def test_schema_makes_two_calls(self):
        text, err = bm.handle_call("schema", {})
        self.assertFalse(err)
        self.assertEqual(len(self.calls), 2)
        self.assertIn(".show tables", self.calls[0])
        self.assertIn("default | getschema", self.calls[1])

    def test_unknown_tool(self):
        text, err = bm.handle_call("does_not_exist", {})
        self.assertTrue(err)
        self.assertIn("unknown tool", text)

    # ---- learning loop ----
    def test_save_then_list_then_run(self):
        text, err = bm.handle_call("save_query", {
            "name": "Big Errors", "description": "errors over a day",
            "kql": "default | where severity_text=='ERROR' | count", "since": "1d ago"})
        self.assertFalse(err)
        # name is sanitized to snake_case
        text, err = bm.handle_call("list_saved", {})
        self.assertIn("big_errors", text)
        # run it back
        self.calls.clear()
        text, err = bm.handle_call("run_saved", {"name": "big_errors"})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], "default | where severity_text=='ERROR' | count")
        self.assertEqual(self.calls[-1][-1], "1d ago")

    def test_save_not_persisted_when_query_fails(self):
        def failing(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return ("PARSE ERROR", True)
        bm.run_bzrk = failing
        text, err = bm.handle_call("save_query", {
            "name": "broken", "description": "x", "kql": "default | nonsense"})
        self.assertTrue(err)
        self.assertIn("NOT saved", text)
        self.assertEqual(bm.load_learned(), [])

    def test_run_saved_missing(self):
        text, err = bm.handle_call("run_saved", {"name": "nope"})
        self.assertTrue(err)
        self.assertIn("No saved query", text)

    def test_learned_store_capped_at_500(self):
        bm.save_learned([{"name": f"q{i}", "description": "x", "kql": "default | count"} for i in range(600)])
        bm.handle_call("save_query", {"name": "one_more", "description": "x", "kql": "default | count"})
        self.assertEqual(len(bm.load_learned()), 500)

    # ---- JSON-RPC protocol ----
    def test_initialize_defaults_to_current_protocol(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "berserk-q")
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertTrue(resp["result"]["instructions"])

    def test_initialize_echoes_client_protocol(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")

    def test_tools_list_count_and_metadata(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(len(names), len(bm.TOOLS) + len(bm.MGMT_TOOLS))
        self.assertIn("search", names)
        self.assertIn("save_query", names)
        # every tool has title, description, inputSchema, and annotations
        for t in resp["result"]["tools"]:
            self.assertTrue(t["title"], t["name"])
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIn("readOnlyHint", t["annotations"])

    def test_annotations_read_only_except_save(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        ann = {t["name"]: t["annotations"] for t in resp["result"]["tools"]}
        for n in ("top_cpu", "errors_by_service", "search", "run_saved",
                  "claude_errors", "logs_for_service", "schema"):
            self.assertTrue(ann[n]["readOnlyHint"], n)
        # save_query writes the local store -> not read-only
        self.assertFalse(ann["save_query"]["readOnlyHint"])
        # list_saved only touches the local store -> not open-world
        self.assertFalse(ann["list_saved"]["openWorldHint"])
        self.assertTrue(ann["top_cpu"]["openWorldHint"])

    def test_tools_call_shape(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "list_hosts", "arguments": {}}})
        self.assertEqual(resp["result"]["content"][0]["type"], "text")
        self.assertFalse(resp["result"]["isError"])

    def test_ping(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        self.assertEqual(resp["result"], {})

    def test_notification_returns_none(self):
        self.assertIsNone(bm.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_errors(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 5, "method": "no/such"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_no_secret_in_descriptions(self):
        """Sanity: no homelab IPs/usernames leaked into tool metadata."""
        blob = json.dumps(bm.TOOLS) + json.dumps(bm.MGMT_TOOLS)
        for leak in ("192.168.", "/opt/assistant", "/home/assistant", "HermesRuntime", "OpenClaw"):
            self.assertNotIn(leak, blob, leak)


if __name__ == "__main__":
    unittest.main(verbosity=2)

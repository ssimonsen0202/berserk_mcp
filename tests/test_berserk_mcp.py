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

    def test_search_rejects_kql_not_starting_with_table(self):
        text, err = bm.handle_call("search", {"kql": "--profile x"})
        self.assertTrue(err)
        self.assertIn("invalid KQL", text)
        self.assertEqual(self.calls, [])  # must not have shelled out

    def test_search_accepts_kql_starting_with_table(self):
        text, err = bm.handle_call("search", {"kql": f"{bm.TABLE} | take 1"})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], f"{bm.TABLE} | take 1")

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
        self.assertEqual(
            bm.Q_CONTAINER_HOSTS,
            "default | where isnotempty(resource['container.name']) "
            "| summarize last_seen=max(timestamp) by "
            "container=tostring(resource['container.name']), host=tostring(resource['host.name']) "
            "| sort by host asc, container asc",
        )

    def test_container_hosts_callable(self):
        text, err = bm.handle_call("container_hosts", {})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], bm.Q_CONTAINER_HOSTS)
        self.assertEqual(self.calls[-1][-1], "1h ago")

    def test_list_metrics_callable(self):
        text, err = bm.handle_call("list_metrics", {})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], bm.Q_METRICS)

    def test_bzrk_query_perf_callable(self):
        text, err = bm.handle_call("bzrk_query_perf", {})
        self.assertFalse(err)
        self.assertIn("$raw", self.calls[-1][3])
        self.assertIn("bzrk.query.execution_duration", self.calls[-1][3])

    def test_discover_schema_no_service(self):
        text, err = bm.handle_call("discover_schema", {})
        self.assertFalse(err)
        # makes TWO calls: bag_keys then row sample
        self.assertEqual(len(self.calls), 2)
        self.assertIn("bag_keys(resource)", self.calls[0][3])
        self.assertIn("take 6", self.calls[1][3])
        # neither call filters by service when none given
        for c in self.calls:
            self.assertNotIn("service.name", c[3])

    def test_discover_schema_with_service(self):
        text, err = bm.handle_call("discover_schema", {"service": "haproxy"})
        self.assertFalse(err)
        for c in self.calls:
            self.assertIn("resource['service.name'] == 'haproxy'", c[3])

    def test_discover_schema_rejects_bad_service(self):
        text, err = bm.handle_call("discover_schema", {"service": "a'; drop"})
        self.assertTrue(err)
        self.assertEqual(self.calls, [])  # must not shell out

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

    def test_save_query_refuses_silent_overwrite(self):
        bm.handle_call("save_query", {
            "name": "dup", "description": "first", "kql": "default | count"})
        text, err = bm.handle_call("save_query", {
            "name": "dup", "description": "second", "kql": "default | take 1"})
        self.assertTrue(err)
        self.assertIn("already exists", text)
        self.assertIn("overwrite=true", text)
        # original entry must be untouched
        items = bm.load_learned()
        match = next(it for it in items if it["name"] == "dup")
        self.assertEqual(match["description"], "first")

    def test_save_query_overwrite_requires_real_boolean(self):
        bm.handle_call("save_query", {
            "name": "dup", "description": "first", "kql": "default | count"})
        # The string "false" is truthy in Python but must not authorize overwrite.
        text, err = bm.handle_call("save_query", {
            "name": "dup", "description": "second", "kql": "default | take 1",
            "overwrite": "false"})
        self.assertTrue(err)
        items = bm.load_learned()
        match = next(it for it in items if it["name"] == "dup")
        self.assertEqual(match["description"], "first")

    def test_save_query_overwrite_true_replaces_entry(self):
        bm.handle_call("save_query", {
            "name": "dup", "description": "first", "kql": "default | count"})
        text, err = bm.handle_call("save_query", {
            "name": "dup", "description": "second", "kql": "default | take 1",
            "overwrite": True})
        self.assertFalse(err)
        items = bm.load_learned()
        match = next(it for it in items if it["name"] == "dup")
        self.assertEqual(match["description"], "second")
        self.assertEqual(match["kql"], "default | take 1")

    def test_run_saved_missing(self):
        text, err = bm.handle_call("run_saved", {"name": "nope"})
        self.assertTrue(err)
        self.assertIn("No saved query", text)

    def test_learned_store_capped_at_500(self):
        bm.save_learned([{"name": f"q{i}", "description": "x", "kql": "default | count"} for i in range(600)])
        bm.handle_call("save_query", {"name": "one_more", "description": "x", "kql": "default | count"})
        self.assertEqual(len(bm.load_learned()), 500)

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits only")
    def test_saved_store_has_private_permissions(self):
        bm.handle_call("save_query", {
            "name": "perms", "description": "x", "kql": "default | count"})
        self.assertEqual(oct(bm.LEARNED_PATH.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(bm.LEARNED_PATH.parent.stat().st_mode & 0o777), oct(0o700))

    def test_amendments_log_capped_at_1000(self):
        amendments_path = Path(bm.LEARNED_PATH).parent / "amendments_log.json"
        bm.save_json_list(amendments_path, [{"ts": "x", "name": f"q{i}"} for i in range(1200)])
        bm.handle_call("save_query", {"name": "one_more", "description": "x", "kql": "default | count"})
        self.assertEqual(len(bm.load_json_list(amendments_path)), 1000)

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


class RoleFilterTest(unittest.TestCase):
    """tool_visible / item_visible / tools-list filtering by ACTIVE_ROLE."""

    def setUp(self):
        self._orig_role = bm.ACTIVE_ROLE
        self.calls = []

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return ("OK", False)

        self._orig_run = bm.run_bzrk
        bm.run_bzrk = fake_run_bzrk

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_learned = bm.LEARNED_PATH
        self._orig_queue = bm.DISCOVERY_QUEUE_PATH
        bm.LEARNED_PATH = Path(self._tmp.name) / "learned.json"
        bm.DISCOVERY_QUEUE_PATH = Path(self._tmp.name) / "queue.json"

    def tearDown(self):
        bm.ACTIVE_ROLE = self._orig_role
        bm.run_bzrk = self._orig_run
        bm.LEARNED_PATH = self._orig_learned
        bm.DISCOVERY_QUEUE_PATH = self._orig_queue
        self._tmp.cleanup()

    def _list_names(self, role):
        bm.ACTIVE_ROLE = role
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {t["name"] for t in resp["result"]["tools"]}

    def test_all_role_sees_everything(self):
        names = self._list_names("all")
        # sre and soc tagged tools must appear when role=all
        self.assertIn("sre_error_rate", names)
        self.assertIn("soc_high_severity_logs", names)
        self.assertIn("claude_errors", names)

    def test_sre_role_sees_sre_not_soc_or_claude(self):
        names = self._list_names("sre")
        self.assertIn("sre_error_rate", names)
        self.assertIn("sre_service_health", names)
        self.assertNotIn("soc_high_severity_logs", names)
        self.assertNotIn("claude_errors", names)
        # untagged tools (no "roles" key) always visible
        self.assertIn("list_hosts", names)

    def test_soc_role_sees_soc_not_sre_or_claude(self):
        names = self._list_names("soc")
        self.assertIn("soc_high_severity_logs", names)
        self.assertIn("soc_timeline", names)
        self.assertNotIn("sre_error_rate", names)
        self.assertNotIn("claude_errors", names)

    def test_claude_role_sees_claude_not_sre_or_soc(self):
        names = self._list_names("claude")
        self.assertIn("claude_errors", names)
        self.assertNotIn("sre_error_rate", names)
        self.assertNotIn("soc_high_severity_logs", names)

    def test_untagged_tools_always_visible(self):
        for role in ("sre", "soc", "claude", "ops"):
            names = self._list_names(role)
            for always in ("list_hosts", "errors_by_service", "search", "save_query"):
                self.assertIn(always, names, f"role={role} missing {always}")

    def test_list_saved_filters_by_role(self):
        bm.ACTIVE_ROLE = "sre"
        bm.save_learned([
            {"name": "sre_q", "description": "SRE query", "kql": "x", "roles": ["sre"]},
            {"name": "soc_q", "description": "SOC query", "kql": "y", "roles": ["soc"]},
            {"name": "any_q", "description": "open query", "kql": "z"},
        ])
        text, err = bm.handle_call("list_saved", {})
        self.assertFalse(err)
        self.assertIn("sre_q", text)
        self.assertNotIn("soc_q", text)
        self.assertIn("any_q", text)

    def test_save_query_attaches_role(self):
        bm.ACTIVE_ROLE = "soc"
        bm.handle_call("save_query", {"name": "myq", "description": "test", "kql": f"{bm.TABLE} | take 1"})
        items = bm.load_learned()
        match = next((it for it in items if it["name"] == "myq"), None)
        self.assertIsNotNone(match)
        # normalize_roles falls back to ACTIVE_ROLE when no roles arg given
        self.assertEqual(match.get("roles"), ["soc"])


class DiscoveryToolTest(unittest.TestCase):
    """request_discovery / discovery_status handlers."""

    def setUp(self):
        self.calls = []
        self._orig_role = bm.ACTIVE_ROLE
        bm.ACTIVE_ROLE = "sre"

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            # request_discovery's visibility check now runs a `summarize
            # n=count()` query and reads the trailing numeric token, so the
            # canned result must look like a count, not a raw service name.
            return (self._search_result, False)

        self._search_result = "1"
        self._orig_run = bm.run_bzrk
        bm.run_bzrk = fake_run_bzrk

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_queue = bm.DISCOVERY_QUEUE_PATH
        bm.DISCOVERY_QUEUE_PATH = Path(self._tmp.name) / "queue.json"

    def tearDown(self):
        bm.ACTIVE_ROLE = self._orig_role
        bm.run_bzrk = self._orig_run
        bm.DISCOVERY_QUEUE_PATH = self._orig_queue
        self._tmp.cleanup()

    def test_request_discovery_queues_service(self):
        text, err = bm.handle_call("request_discovery", {"service": "my-new-service"})
        self.assertFalse(err, text)
        self.assertIn("queued", text)
        queue = bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["source"], "my-new-service")
        self.assertEqual(queue[0]["kind"], "service")
        self.assertEqual(queue[0]["status"], "pending")

    def test_request_discovery_deduplicates(self):
        bm.handle_call("request_discovery", {"service": "my-new-service"})
        bm.handle_call("request_discovery", {"service": "my-new-service"})
        queue = bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)
        self.assertEqual(len(queue), 1)

    def test_request_discovery_rejects_both_service_and_metric(self):
        text, err = bm.handle_call("request_discovery", {"service": "s", "metric": "m"})
        self.assertTrue(err)
        self.assertIn("exactly one", text)

    def test_request_discovery_rejects_neither(self):
        text, err = bm.handle_call("request_discovery", {})
        self.assertTrue(err)

    def test_request_discovery_rejects_invalid_name(self):
        text, err = bm.handle_call("request_discovery", {"service": "bad name!"})
        self.assertTrue(err)

    def test_request_discovery_rejects_unseen_source(self):
        self._search_result = "0"  # count query reports zero matches
        text, err = bm.handle_call("request_discovery", {"service": "my-new-service"})
        self.assertTrue(err)
        self.assertIn("not currently visible", text)

    def test_discovery_queue_capped_at_500(self):
        bm.save_json_list(bm.DISCOVERY_QUEUE_PATH, [
            {"source": f"svc{i}", "kind": "service", "status": "done"} for i in range(600)
        ])
        bm.handle_call("request_discovery", {"service": "my-new-service"})
        self.assertEqual(len(bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)), 500)

    def test_discovery_status_empty(self):
        text, err = bm.handle_call("discovery_status", {})
        self.assertFalse(err)
        self.assertIn("No discovery jobs", text)

    def test_discovery_status_lists_jobs(self):
        bm.handle_call("request_discovery", {"service": "my-new-service"})
        text, err = bm.handle_call("discovery_status", {})
        self.assertFalse(err)
        self.assertIn("my-new-service", text)
        self.assertIn("pending", text)

    def test_sre_service_health_dispatches(self):
        text, err = bm.handle_call("sre_service_health", {"service": "api-gateway"})
        self.assertFalse(err)
        kql_arg = self.calls[-1][3]
        self.assertIn("api-gateway", kql_arg)

    def test_soc_timeline_dispatches(self):
        text, err = bm.handle_call("soc_timeline", {"service": "api-gateway"})
        self.assertFalse(err)
        kql_arg = self.calls[-1][3]
        self.assertIn("api-gateway", kql_arg)

    def test_sre_service_health_rejects_bad_service(self):
        _, err = bm.handle_call("sre_service_health", {"service": "bad name!"})
        self.assertTrue(err)

    def test_soc_timeline_rejects_bad_service(self):
        _, err = bm.handle_call("soc_timeline", {"service": "bad name!"})
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main(verbosity=2)

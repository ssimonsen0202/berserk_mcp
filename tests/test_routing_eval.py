"""Routing eval: verify that the right tool is dispatched for natural-language-style inputs.

These tests simulate what a cheap LLM (gpt-4.1-mini) would call after reading tool descriptions
and a user prompt. They do NOT call an LLM — they assert that handle_call dispatches correctly
and returns plausible output, proving the tool descriptions are unambiguous enough to route to.

Each test is named after the prompt intent. The "expected tool" is what the model should call.
Run with BERSERK_MCP_ROLE=sre / soc / claude to verify lane-specific routing works.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm


class RoutingEvalBase(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return ("row1\nrow2\nrow3", False)

        self._orig_run = bm.run_bzrk
        bm.run_bzrk = fake_run_bzrk
        self._orig_role = bm.ACTIVE_ROLE

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_learned = bm.LEARNED_PATH
        self._orig_queue = bm.DISCOVERY_QUEUE_PATH
        bm.LEARNED_PATH = Path(self._tmp.name) / "learned.json"
        bm.DISCOVERY_QUEUE_PATH = Path(self._tmp.name) / "queue.json"

    def tearDown(self):
        bm.run_bzrk = self._orig_run
        bm.ACTIVE_ROLE = self._orig_role
        bm.LEARNED_PATH = self._orig_learned
        bm.DISCOVERY_QUEUE_PATH = self._orig_queue
        self._tmp.cleanup()

    def _kql(self):
        return self.calls[-1][3] if self.calls else ""


class SRERoutingEval(RoutingEvalBase):
    """9 SRE-lane routing assertions. Each maps an intent to the expected tool + KQL shape."""

    def setUp(self):
        super().setUp()
        bm.ACTIVE_ROLE = "sre"

    # --- error rate ---
    def test_is_error_rate_climbing(self):
        # "show me error rate per minute per service"
        text, err = bm.handle_call("sre_error_rate", {})
        self.assertFalse(err)
        self.assertIn("severity_text == 'ERROR'", self._kql())
        self.assertIn("make-series", self._kql())

    def test_error_rate_with_since(self):
        # "error rate for the last 30 minutes"
        text, err = bm.handle_call("sre_error_rate", {"since": "30m ago"})
        self.assertFalse(err)
        self.assertIn("30m ago", self.calls[-1])

    # --- host headroom ---
    def test_which_host_has_cpu_pressure(self):
        # "which host has the most CPU pressure"
        text, err = bm.handle_call("sre_host_headroom", {})
        self.assertFalse(err)
        self.assertIn("system.cpu.load_average.1m", self._kql())
        self.assertIn("system.memory.usage", self._kql())

    # --- ingest health ---
    def test_is_berserk_falling_behind(self):
        # "is Berserk healthy / is it keeping up with ingestion"
        text, err = bm.handle_call("sre_ingest_health", {})
        self.assertFalse(err)
        self.assertIn("bzrk.nursery.ingest_lag_seconds", self._kql())
        self.assertIn("bzrk.ingest.data_dropped", self._kql())

    # --- top error messages ---
    def test_what_are_the_worst_error_messages(self):
        # "what are the most common error messages across all services"
        text, err = bm.handle_call("sre_top_error_messages", {})
        self.assertFalse(err)
        self.assertIn("hits=count()", self._kql())
        self.assertIn("take 40", self._kql())
        self.assertIn("extract_log_template", self._kql())
        self.assertIn("example=", self._kql())

    # --- service health (parameterized) ---
    def test_how_is_specific_service_doing(self):
        # "how is api-gateway doing overall"
        text, err = bm.handle_call("sre_service_health", {"service": "api-gateway"})
        self.assertFalse(err)
        self.assertIn("api-gateway", self._kql())
        self.assertIn("errors=countif", self._kql())

    def test_service_health_bad_service_rejected(self):
        # injection guard fires before KQL is built
        _, err = bm.handle_call("sre_service_health", {"service": "bad name!"})
        self.assertTrue(err)
        self.assertEqual(len(self.calls), 0)

    # --- fallback to generic search ---
    def test_arbitrary_kql_falls_through_to_search(self):
        # "run this KQL: default | take 1"
        text, err = bm.handle_call("search", {"kql": "default | take 1"})
        self.assertFalse(err)
        self.assertIn("default | take 1", self._kql())

    # --- save then reuse pattern ---
    def test_save_then_run_saved(self):
        # "save this query so I can reuse it"
        bm.handle_call("save_query", {
            "name": "my_sre_check",
            "description": "custom SRE check",
            "kql": "default | take 5",
        })
        text, err = bm.handle_call("run_saved", {"name": "my_sre_check"})
        self.assertFalse(err)


class SOCRoutingEval(RoutingEvalBase):
    """SOC-lane routing assertions."""

    def setUp(self):
        super().setUp()
        bm.ACTIVE_ROLE = "soc"

    def test_any_critical_events_right_now(self):
        # "show me all CRITICAL/FATAL events today"
        text, err = bm.handle_call("soc_high_severity_logs", {})
        self.assertFalse(err)
        self.assertIn("CRITICAL", self._kql())
        self.assertIn("FATAL", self._kql())

    def test_which_services_are_spiking(self):
        # "which services are logging the most right now"
        text, err = bm.handle_call("soc_log_spike", {})
        self.assertFalse(err)
        self.assertIn("hits=count()", self._kql())
        self.assertIn("make-series", self._kql())

    def test_any_new_services(self):
        # "are there any services I haven't seen before"
        text, err = bm.handle_call("soc_new_services", {})
        self.assertFalse(err)
        self.assertIn("first_seen=min(timestamp)", self._kql())

    def test_repeated_errors(self):
        # "what errors keep happening over and over"
        text, err = bm.handle_call("soc_repeated_errors", {})
        self.assertFalse(err)
        self.assertIn("hits > 5", self._kql())
        self.assertIn("extract_log_template", self._kql())

    def test_incident_timeline_for_service(self):
        # "walk me through what payment-svc did in the last hour"
        text, err = bm.handle_call("soc_timeline", {"service": "payment-svc"})
        self.assertFalse(err)
        self.assertIn("payment-svc", self._kql())
        self.assertIn("tail 100", self._kql())


class RoleBoundaryEval(RoutingEvalBase):
    """Verify role-filtered tools/list only exposes what the lane needs."""

    def _names(self, role):
        bm.ACTIVE_ROLE = role
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {t["name"] for t in resp["result"]["tools"]}

    def test_sre_does_not_see_soc_tools(self):
        sre = self._names("sre")
        self.assertNotIn("soc_high_severity_logs", sre)
        self.assertNotIn("soc_timeline", sre)

    def test_soc_does_not_see_sre_tools(self):
        soc = self._names("soc")
        self.assertNotIn("sre_error_rate", soc)
        self.assertNotIn("sre_host_headroom", soc)

    def test_all_sees_both(self):
        both = self._names("all")
        self.assertIn("sre_error_rate", both)
        self.assertIn("soc_high_severity_logs", both)

    def test_sre_tool_count_less_than_all(self):
        self.assertLess(len(self._names("sre")), len(self._names("all")))

    def test_soc_tool_count_less_than_all(self):
        self.assertLess(len(self._names("soc")), len(self._names("all")))


if __name__ == "__main__":
    unittest.main(verbosity=2)

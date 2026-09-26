"""LLM-generated saved queries need an operator's approval before the small tier sees them.

Review 2026-09-26 (docs/mcp-guidance-review-2026-09-26.md), P2: parser-factory
queries, whose KQL, name and description an LLM wrote from untrusted
telemetry, became small-tier tools with no review. Approval is CLI-only.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import berserk_mcp as bm  # noqa: E402


def _generated(name, **extra):
    entry = {
        "name": name,
        "kql": bm.TABLE + " | take 1",
        "since": "1h ago",
        "description": "Counts rows.",
        "origin": "generated",
        "generated_by": {"provider": "p", "model": "m", "ts": "2026-09-26T00:00:00Z"},
    }
    entry.update(extra)
    return entry


class GeneratedApprovalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (bm.LEARNED_PATH, bm.run_bzrk, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED, bm.CACHE_TTL_SECONDS)
        bm.LEARNED_PATH = Path(self._tmp.name) / "learned.json"
        bm.CACHE_TTL_SECONDS = 0
        self.calls = []

        def fake(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return "n\n1", False

        bm.run_bzrk = fake
        self.small()

    def tearDown(self):
        bm.LEARNED_PATH, bm.run_bzrk, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED, bm.CACHE_TTL_SECONDS = self._saved
        self._tmp.cleanup()

    def small(self):
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "ops", bm.TIER_SMALL

    def deep(self):
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "ops", bm.TIER_DEEP

    def store(self, *items):
        bm.save_learned(list(items))

    def listed(self):
        return {t["name"] for t in bm._tool_list_result("legacy")["tools"]}

    # Covers SECURITY.md#trust-boundaries
    def test_pending_generated_query_is_invisible_and_unrunnable_in_the_small_tier(self):
        self.store(_generated("fast_errors", status=bm.GENERATED_PENDING))
        self.assertNotIn("saved__fast_errors", self.listed())
        self.assertNotIn("fast_errors", bm.handle_call("list_saved", {})[0])
        text, is_err = bm.handle_call("run_saved", {"name": "fast_errors"})
        self.assertTrue(is_err)
        self.assertIn("No saved query named 'fast_errors'", text)
        text, is_err = bm.handle_call("saved__fast_errors", {})
        self.assertEqual((text, is_err), ("unknown tool: saved__fast_errors", True))
        self.assertEqual(self.calls, [])

    def test_generated_entry_without_status_counts_as_pending(self):
        # Stores written before the gate have no status field.
        legacy_origin = _generated("old_one")
        legacy_generated_by = _generated("older_one")
        legacy_generated_by.pop("origin")
        self.store(legacy_origin, legacy_generated_by)
        self.assertFalse({"saved__old_one", "saved__older_one"} & self.listed())

    def test_approved_generated_query_is_visible_and_runs(self):
        self.store(_generated("fast_errors", status=bm.GENERATED_APPROVED))
        self.assertIn("saved__fast_errors", self.listed())
        _, is_err = bm.handle_call("run_saved", {"name": "fast_errors"})
        self.assertFalse(is_err)
        self.assertTrue(self.calls)

    def test_human_saved_query_needs_no_approval(self):
        self.store({"name": "mine", "kql": bm.TABLE + " | take 1", "since": "1h ago", "description": "Mine."})
        self.assertIn("saved__mine", self.listed())

    def test_deep_tier_sees_pending_queries(self):
        self.store(_generated("fast_errors", status=bm.GENERATED_PENDING))
        self.deep()
        self.assertIn("saved__fast_errors", self.listed())
        self.assertIn("status=pending", bm.handle_call("review_generated", {})[0])

    def test_generated_write_is_pending_even_when_replacing_an_approved_entry(self):
        self.store(_generated("fast_errors", status=bm.GENERATED_APPROVED, approved_at="2026-09-26T00:00:00Z"))
        bm.persist_learned_query(_generated("fast_errors", kql=bm.TABLE + " | take 2"), action_source="generated")
        entry = next(it for it in bm.load_learned() if it["name"] == "fast_errors")
        self.assertEqual(entry["status"], bm.GENERATED_PENDING)
        self.assertNotIn("approved_at", entry)

    def test_approve_marks_only_generated_entries(self):
        self.store(_generated("fast_errors"), {"name": "mine", "kql": "x", "description": "Mine."})
        entry, error = bm.approve_generated_query("fast_errors")
        self.assertIsNone(error)
        self.assertEqual(entry["status"], bm.GENERATED_APPROVED)
        self.assertIn("approved_at", entry)
        self.assertIn("saved__fast_errors", self.listed())
        self.assertEqual(
            bm.approve_generated_query("mine")[1],
            "'mine' is not a generated query; only generated queries need approval",
        )
        self.assertEqual(bm.approve_generated_query("nope")[1], "no saved query named 'nope'")

    def test_discovery_status_names_only_queries_this_lane_can_run(self):
        self.store(_generated("pend", status=bm.GENERATED_PENDING), _generated("ok_one", status=bm.GENERATED_APPROVED))
        queue = Path(self._tmp.name) / "queue.json"
        queue.write_text(
            json.dumps(
                [
                    {
                        "source": "haproxy",
                        "kind": "service",
                        "status": "done",
                        "report": {"provider": "p", "queries_saved": ["pend", "ok_one"]},
                    }
                ]
            )
        )
        original = bm.DISCOVERY_QUEUE_PATH
        try:
            bm.DISCOVERY_QUEUE_PATH = queue
            text, _ = bm.handle_call("discovery_status", {})
        finally:
            bm.DISCOVERY_QUEUE_PATH = original
        self.assertIn("saved ok_one (1 not available here", text)
        self.assertNotIn("pend", text.replace("pending operator approval", ""))

    def test_malformed_roles_hide_the_entry_instead_of_crashing(self):
        self.store({"name": "x", "kql": "k", "description": "d", "roles": 5})
        self.assertNotIn("saved__x", self.listed())
        self.assertEqual(bm.handle_call("list_saved", {})[0], "No saved queries yet.")

    def test_discovery_status_hides_failure_reasons_and_survives_bad_store_entries(self):
        bm.save_learned([None, {}, {"name": 5}])
        queue = Path(self._tmp.name) / "queue.json"
        queue.write_text(
            json.dumps(
                [
                    {
                        "source": "a",
                        "kind": "service",
                        "status": "failed",
                        "report": {"reason": "persistence failed for secret_pending: OSError"},
                    },
                    {
                        "source": "b",
                        "kind": "service",
                        "status": "done",
                        "report": {"provider": "p", "queries_saved": ["x"]},
                    },
                ]
            )
        )
        original = bm.DISCOVERY_QUEUE_PATH
        try:
            bm.DISCOVERY_QUEUE_PATH = queue
            small_text, is_err = bm.handle_call("discovery_status", {})
            self.deep()
            deep_text, _ = bm.handle_call("discovery_status", {})
        finally:
            bm.DISCOVERY_QUEUE_PATH = original
        self.assertFalse(is_err)
        self.assertNotIn("secret_pending", small_text)
        self.assertIn("details are shown at the deep tier", small_text)
        self.assertIn("saved none usable here (1 not available here", small_text)
        self.assertIn("secret_pending", deep_text)


class ApproveCliTest(unittest.TestCase):
    def _cli(self, store, *args):
        env = dict(os.environ)
        for key in [k for k in env if k.startswith(("BERSERK_MCP_", "BERSERK_LLM_"))]:
            del env[key]
        env.update(
            {"HOME": store, "USERPROFILE": store, "BERSERK_MCP_LEARNED_PATH": os.path.join(store, "learned.json")}
        )
        return subprocess.run(
            [sys.executable, str(REPO / "berserk_mcp.py"), *args], capture_output=True, text=True, env=env, timeout=60
        )

    def test_cli_approves_and_prints_what_it_approved(self):
        with tempfile.TemporaryDirectory() as store:
            Path(store, "learned.json").write_text(json.dumps([_generated("fast_errors")]))
            result = self._cli(store, "--approve-generated", "fast_errors")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("approved 'fast_errors'", result.stdout)
            self.assertIn("kql:", result.stdout)
            saved = json.loads(Path(store, "learned.json").read_text())
            self.assertEqual(saved[0]["status"], "approved")

    def test_cli_output_cannot_carry_terminal_control_sequences(self):
        with tempfile.TemporaryDirectory() as store:
            forged = _generated("fast_errors", kql=bm.TABLE + " | take 1 // \x1b[2J\x1b[HFORGED", description="d\x07")
            Path(store, "learned.json").write_text(json.dumps([forged]))
            result = self._cli(store, "--approve-generated", "fast_errors")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("\x1b", result.stdout)
            self.assertNotIn("\x07", result.stdout)
            self.assertIn("\\x1b[2J", result.stdout)

    def test_cli_refuses_unknown_and_human_entries(self):
        with tempfile.TemporaryDirectory() as store:
            Path(store, "learned.json").write_text(json.dumps([{"name": "mine", "kql": "x", "description": "Mine."}]))
            for name in ("mine", "nope"):
                with self.subTest(name=name):
                    result = self._cli(store, "--approve-generated", name)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("not approved", result.stderr)


if __name__ == "__main__":
    unittest.main()

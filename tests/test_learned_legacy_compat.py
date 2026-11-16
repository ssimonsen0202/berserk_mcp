"""Guard: a learned-query store written by v1.37.5 keeps behaving the same.

tests/fixtures/learned_legacy_v1_37.json was produced by the unchanged v1.37.5
code (save_query, persist_learned_query(..., "generated"), approve_generated_query)
against a temporary store. These tests pin how tools/list, list_saved, run_saved
and saved__<name> treat it, per tier and role, so later changes to the store
format cannot break an existing store without a test failing.
"""

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import berserk_mcp as bm  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "learned_legacy_v1_37.json"

MANUAL = "checkout_errors"
ROLE_SCOPED = "payments_volume"  # roles: ["sre"]
PENDING = "checkout_slow_pending"  # generated, pending approval
APPROVED = "payments_top_approved"  # generated, approved


class LearnedLegacyCompatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        saved = (bm.LEARNED_PATH, bm.run_bzrk, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED, bm.CACHE_TTL_SECONDS)

        def restore():
            bm.LEARNED_PATH, bm.run_bzrk, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED, bm.CACHE_TTL_SECONDS = saved

        self.addCleanup(restore)
        self.store_path = Path(self._tmp.name) / "learned.json"
        shutil.copyfile(FIXTURE, self.store_path)
        bm.LEARNED_PATH = self.store_path
        bm.CACHE_TTL_SECONDS = 0
        self.calls = []

        def fake(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return "n\n7", False

        bm.run_bzrk = fake
        self.use("all", bm.TIER_DEEP)

    def use(self, role, tier):
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = role, tier

    def projected(self):
        return {t["name"]: t for t in bm._saved_query_tools()}

    def listed_tools(self):
        return {t["name"] for t in bm._tool_list_result("legacy")["tools"] if t["name"].startswith("saved__")}

    def listed_saved(self):
        text, is_err = bm.handle_call("list_saved", {})
        self.assertFalse(is_err)
        return text

    # ---- the fixture itself ----
    def test_fixture_has_the_four_legacy_shapes(self):
        by_name = {it["name"]: it for it in bm.load_learned()}
        self.assertEqual(set(by_name), {MANUAL, ROLE_SCOPED, PENDING, APPROVED})
        self.assertNotIn("origin", by_name[MANUAL])
        self.assertEqual(by_name[ROLE_SCOPED]["roles"], ["sre"])
        self.assertEqual((by_name[PENDING]["origin"], by_name[PENDING]["status"]), ("generated", "pending"))
        self.assertEqual((by_name[APPROVED]["origin"], by_name[APPROVED]["status"]), ("generated", "approved"))
        self.assertIn("approved_at", by_name[APPROVED])

    # ---- tools/list projection ----
    def test_deep_tier_projects_every_entry(self):
        expected = {"saved__" + n for n in (MANUAL, ROLE_SCOPED, PENDING, APPROVED)}
        self.assertEqual(set(self.projected()), expected)
        self.assertEqual(self.listed_tools(), expected)

    def test_small_tier_hides_the_pending_generated_entry(self):
        self.use("all", bm.TIER_SMALL)
        expected = {"saved__" + n for n in (MANUAL, ROLE_SCOPED, APPROVED)}
        self.assertEqual(set(self.projected()), expected)
        self.assertEqual(self.listed_tools(), expected)

    def test_other_role_does_not_see_the_role_scoped_entry(self):
        for tier, names in (
            (bm.TIER_DEEP, {MANUAL, PENDING, APPROVED}),
            (bm.TIER_SMALL, {MANUAL, APPROVED}),
        ):
            self.use("ops", tier)
            expected = {"saved__" + n for n in names}
            self.assertEqual(set(self.projected()), expected, tier)
            self.assertEqual(self.listed_tools(), expected, tier)

    def test_matching_role_sees_the_role_scoped_entry(self):
        self.use("sre", bm.TIER_SMALL)
        self.assertIn("saved__" + ROLE_SCOPED, self.projected())
        self.assertIn("saved__" + ROLE_SCOPED, self.listed_tools())

    def test_generated_descriptions_are_fenced_and_manual_ones_are_not(self):
        tools = self.projected()
        for name in (PENDING, APPROVED):
            desc = tools["saved__" + name]["description"]
            self.assertTrue(desc.startswith("<generated-description>"), name)
            self.assertTrue(desc.endswith("</generated-description>"), name)
        for name in (MANUAL, ROLE_SCOPED):
            self.assertNotIn("generated-description", tools["saved__" + name]["description"], name)

    def test_input_schema_is_since_only(self):
        for tool in self.projected().values():
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertEqual(list(schema["properties"]), ["since"])
        for item in bm._tool_list_result("legacy")["tools"]:
            if item["name"].startswith("saved__"):
                self.assertEqual(list(item["inputSchema"]["properties"]), ["since"], item["name"])

    # ---- list_saved ----
    def test_list_saved_per_tier(self):
        text = self.listed_saved()
        for name in (MANUAL, ROLE_SCOPED, PENDING, APPROVED):
            self.assertIn("- " + name + ":", text)
        self.assertIn("<generated-description>Slow checkout spans, awaiting approval.</generated-description>", text)
        self.assertNotIn("<generated-description>Errors from", text)
        self.use("all", bm.TIER_SMALL)
        text = self.listed_saved()
        self.assertNotIn(PENDING, text)
        for name in (MANUAL, ROLE_SCOPED, APPROVED):
            self.assertIn("- " + name + ":", text)

    def test_list_saved_hides_the_role_scoped_entry_from_another_role(self):
        self.use("ops", bm.TIER_DEEP)
        text = self.listed_saved()
        self.assertNotIn(ROLE_SCOPED, text)
        for name in (MANUAL, PENDING, APPROVED):
            self.assertIn("- " + name + ":", text)

    # ---- run_saved and saved__<name> ----
    def test_visible_entries_run_through_both_paths(self):
        self.use("sre", bm.TIER_SMALL)
        for name in (MANUAL, APPROVED, ROLE_SCOPED):
            for tool, args in (("run_saved", {"name": name}), ("saved__" + name, {})):
                self.calls.clear()
                text, is_err = bm.handle_call(tool, args)
                self.assertFalse(is_err, (tool, name, text))
                self.assertIn("7", text, (tool, name))
                self.assertTrue(self.calls, (tool, name))

    def test_run_saved_uses_the_stored_kql_and_since(self):
        bm.handle_call("run_saved", {"name": MANUAL})
        self.assertIn("default | where resource['service.name'] == 'checkout' | take 10", self.calls[-1])
        self.assertIn("1h ago", self.calls[-1])
        bm.handle_call("run_saved", {"name": MANUAL, "since": "2h ago"})
        self.assertIn("2h ago", self.calls[-1])

    def test_pending_generated_entry_is_not_runnable_in_the_small_tier(self):
        self.use("all", bm.TIER_SMALL)
        self.calls.clear()
        text, is_err = bm.handle_call("run_saved", {"name": PENDING})
        self.assertTrue(is_err)
        self.assertIn("No saved query named '" + PENDING + "'", text)
        self.assertEqual(
            bm.handle_call("saved__" + PENDING, {}),
            ("unknown tool: saved__" + PENDING, True),
        )
        self.assertEqual(self.calls, [])

    def test_pending_generated_entry_runs_in_the_deep_tier(self):
        for tool, args in (("run_saved", {"name": PENDING}), ("saved__" + PENDING, {})):
            text, is_err = bm.handle_call(tool, args)
            self.assertFalse(is_err, tool)
            self.assertIn("7", text)

    def test_role_hidden_entry_gives_the_not_found_reply(self):
        self.use("ops", bm.TIER_DEEP)
        self.calls.clear()
        text, is_err = bm.handle_call("run_saved", {"name": ROLE_SCOPED})
        self.assertTrue(is_err)
        self.assertIn("No saved query named '" + ROLE_SCOPED + "'", text)
        self.assertNotIn(ROLE_SCOPED, text.split("Available:")[1])
        self.assertEqual(
            bm.handle_call("saved__" + ROLE_SCOPED, {}),
            ("unknown tool: saved__" + ROLE_SCOPED, True),
        )
        self.assertEqual(self.calls, [])

    # ---- reading never writes ----
    def test_reading_and_running_do_not_modify_the_store(self):
        before = hashlib.sha256(self.store_path.read_bytes()).hexdigest()
        for tier in (bm.TIER_DEEP, bm.TIER_SMALL):
            for role in ("all", "ops", "sre"):
                self.use(role, tier)
                bm._saved_query_tools()
                bm._tool_list_result("legacy")
                bm.handle_call("list_saved", {})
                bm.handle_call("run_saved", {"name": MANUAL})
                bm.handle_call("saved__" + APPROVED, {})
        self.assertEqual(hashlib.sha256(self.store_path.read_bytes()).hexdigest(), before)
        self.assertEqual(self.store_path.read_bytes(), FIXTURE.read_bytes())


if __name__ == "__main__":
    unittest.main()

"""Role-expansion acceptance tests for roadmap Phase D."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402


class RoleExpansionTest(unittest.TestCase):
    def setUp(self):
        self._orig_role = bm.ACTIVE_ROLE
        self._orig_primers_dir = os.environ.get("BERSERK_MCP_PRIMERS_DIR")
        self._orig_tools_len = len(bm.TOOLS)
        self._added_roles = []
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        bm.ACTIVE_ROLE = self._orig_role
        del bm.TOOLS[self._orig_tools_len :]
        for role in self._added_roles:
            bm._ROLE_PREFIX.pop(role, None)
        if self._orig_primers_dir is None:
            os.environ.pop("BERSERK_MCP_PRIMERS_DIR", None)
        else:
            os.environ["BERSERK_MCP_PRIMERS_DIR"] = self._orig_primers_dir
        self._tmp.cleanup()

    def test_novel_role_flows_without_dispatch_changes(self):
        role = "incident-response"
        self._added_roles.append(role)
        bm._ROLE_PREFIX[role] = "You are in the incident-response lane. "
        Path(self._tmp.name, f"{role}.md").write_text(
            "# Incident-response primer\n\nUse the incident timeline first.\n",
            encoding="utf-8",
        )
        os.environ["BERSERK_MCP_PRIMERS_DIR"] = self._tmp.name

        bm.ACTIVE_ROLE = role
        bm.TOOLS.append({
            "name": "incident_timeline",
            "roles": [role],
            "description": "A test-only role tool.",
            "inputSchema": {"type": "object", "properties": {}},
        })

        instructions = bm.build_instructions(role)
        response = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}

        self.assertIn("Incident-response primer", instructions)
        self.assertIn("incident-response lane", instructions)
        self.assertIn("incident_timeline", names)
        self.assertNotIn("sre_error_rate", names)
        self.assertEqual(bm.normalize_roles(role), [role])

    def test_windows_forensics_is_a_registered_stub_lane(self):
        role = "windows-forensics"
        self.assertIn(role, bm._ROLE_PREFIX)
        primer = Path(bm.__file__).resolve().parent / "primers" / f"{role}.md"
        text = primer.read_text(encoding="utf-8")

        self.assertIn("Windows Security", text)
        self.assertIn("Sysmon", text)
        self.assertIn("discover_schema", text)
        self.assertIn("no fixed `win_*` tools", text)
        self.assertFalse(
            any(tool["name"].startswith("win_") for tool in bm.TOOLS),
            "schema-gated Windows tools must not ship before live field verification",
        )

        bm.ACTIVE_ROLE = role
        response = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("discover_schema", names)
        self.assertIn("suggest_ingestion", names)
        self.assertNotIn("sre_error_rate", names)
        self.assertNotIn("soc_high_severity_logs", names)


class ToolTierResolutionTest(unittest.TestCase):
    """FR-2: pure resolution logic, tested directly -- no import/reload
    needed since _resolve_tier takes its inputs as arguments."""

    def test_explicit_small_wins_even_on_role_all(self):
        self.assertEqual(bm._resolve_tier("small", "all"), bm.TIER_SMALL)

    def test_explicit_deep_wins_even_on_a_single_lane(self):
        self.assertEqual(bm._resolve_tier("deep", "sre"), bm.TIER_DEEP)

    def test_unset_and_role_all_defaults_to_deep(self):
        self.assertEqual(bm._resolve_tier("", "all"), bm.TIER_DEEP)

    def test_unset_and_single_lane_defaults_to_small(self):
        self.assertEqual(bm._resolve_tier("", "sre"), bm.TIER_SMALL)
        self.assertEqual(bm._resolve_tier("", "soc"), bm.TIER_SMALL)
        self.assertEqual(bm._resolve_tier("", "windows-forensics"), bm.TIER_SMALL)


class ToolTierVisibilityTest(unittest.TestCase):
    """FR-3: tool_visible enforces both lane and tier. Tests monkeypatch
    ACTIVE_ROLE and ACTIVE_TIER_RESOLVED directly (same pattern
    RoleExpansionTest already uses for ACTIVE_ROLE) rather than
    importlib.reload -- tool_visible only ever reads these two module
    globals at call time, so this exercises the real code path without
    the risk of a full-module reload re-running import-time side effects
    (F-008's sys.exit check, INSTRUCTIONS rebuild, etc.)."""

    def setUp(self):
        self._orig_role = bm.ACTIVE_ROLE
        self._orig_tier = bm.ACTIVE_TIER_RESOLVED

    def tearDown(self):
        bm.ACTIVE_ROLE = self._orig_role
        bm.ACTIVE_TIER_RESOLVED = self._orig_tier

    def _visible_names(self):
        response = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {tool["name"] for tool in response["result"]["tools"]}

    def test_role_all_tier_unset_is_byte_identical_surface(self):
        # Backwards-compatibility guarantee: role=all with no explicit tier
        # resolves to deep, so every deep-tier tool is still present.
        bm.ACTIVE_ROLE = "all"
        bm.ACTIVE_TIER_RESOLVED = bm._resolve_tier("", "all")
        names = self._visible_names()
        for tool_name in bm._DEEP_TIER_TOOLS:
            self.assertIn(tool_name, names, f"{tool_name} missing from role=all surface")

    def test_single_lane_tier_unset_hides_every_deep_tool(self):
        bm.ACTIVE_ROLE = "sre"
        bm.ACTIVE_TIER_RESOLVED = bm._resolve_tier("", "sre")
        names = self._visible_names()
        for tool_name in bm._DEEP_TIER_TOOLS:
            self.assertNotIn(tool_name, names, f"{tool_name} should be tier-hidden for sre")

    def test_explicit_deep_on_a_single_lane_restores_deep_tools(self):
        bm.ACTIVE_ROLE = "sre"
        bm.ACTIVE_TIER_RESOLVED = bm._resolve_tier("deep", "sre")
        names = self._visible_names()
        # None of _DEEP_TIER_TOOLS carry role tags that would exclude sre
        # (validate_kql is tagged with all four operational roles), so the
        # full set should reappear.
        for tool_name in bm._DEEP_TIER_TOOLS:
            self.assertIn(tool_name, names, f"{tool_name} should reappear under explicit deep")

    def test_explicit_small_on_role_all_still_hides_deep_tools(self):
        # FR-2 rule 1 (explicit tier) beats rule 2 (role==all -> deep).
        bm.ACTIVE_ROLE = "all"
        bm.ACTIVE_TIER_RESOLVED = bm._resolve_tier("small", "all")
        names = self._visible_names()
        for tool_name in bm._DEEP_TIER_TOOLS:
            self.assertNotIn(tool_name, names, f"{tool_name} should be hidden under explicit small")

    def test_tier_hidden_tool_call_returns_unknown_tool_like_role_hidden(self):
        # F-008's own rule: a hidden tool must not leak that it exists.
        bm.ACTIVE_ROLE = "sre"
        bm.ACTIVE_TIER_RESOLVED = bm._resolve_tier("", "sre")
        response = bm.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search", "arguments": {"kql": "default | take 1"}},
        })
        content = response["result"]["content"][0]["text"]
        self.assertEqual(content, "unknown tool: search")
        self.assertTrue(response["result"].get("isError"))

    def test_small_tier_keeps_fixed_intent_tools_visible(self):
        # list_saved/run_saved/discover_schema and a saved__* projection
        # are deliberately NOT in _DEEP_TIER_TOOLS -- running a verified
        # saved query, or reading a schema, is fixed-intent, exactly what
        # the small tier is for.
        orig_learned = bm.LEARNED_PATH
        import tempfile
        from pathlib import Path as _Path
        tmp = tempfile.TemporaryDirectory()
        try:
            bm.LEARNED_PATH = _Path(tmp.name) / "learned.json"
            bm.persist_learned_query(
                {"name": "tier_test_probe", "description": "d", "kql": "default | take 1"},
                action_source="manual",
            )
            bm.ACTIVE_ROLE = "sre"
            bm.ACTIVE_TIER_RESOLVED = bm._resolve_tier("", "sre")
            names = self._visible_names()
            self.assertIn("list_saved", names)
            self.assertIn("run_saved", names)
            self.assertIn("discover_schema", names)
            self.assertIn("saved__tier_test_probe", names)
        finally:
            bm.LEARNED_PATH = orig_learned
            tmp.cleanup()


class ToolTierStartupLogTest(unittest.TestCase):
    """FR-4: the hidden-tool announcement is built by a pure function
    (_tier_hidden_announcement), tested directly rather than by capturing
    real log() output at import time."""

    def test_small_tier_names_count_and_escape_hatch(self):
        message = bm._tier_hidden_announcement(bm.TIER_SMALL, "sre")
        self.assertIsNotNone(message)
        self.assertIn(str(len(bm._DEEP_TIER_TOOLS)), message)
        self.assertIn("search", message)
        self.assertIn("BERSERK_MCP_TIER=deep", message)

    def test_deep_tier_has_no_announcement(self):
        self.assertIsNone(bm._tier_hidden_announcement(bm.TIER_DEEP, "all"))


class ToolTierDoctorCheckTest(unittest.TestCase):
    """FR-5: self_check/--doctor reports the resolved tier."""

    def setUp(self):
        self._orig_role = bm.ACTIVE_ROLE
        self._orig_tier = bm.ACTIVE_TIER_RESOLVED

    def tearDown(self):
        bm.ACTIVE_ROLE = self._orig_role
        bm.ACTIVE_TIER_RESOLVED = self._orig_tier

    def test_reports_resolved_tier_as_a_passing_optional_check(self):
        bm.ACTIVE_ROLE = "sre"
        bm.ACTIVE_TIER_RESOLVED = bm._resolve_tier("", "sre")
        result = bm._doctor_check_tool_tier()
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["required"])
        self.assertIn("small", result["detail"])
        self.assertIn(str(len(bm._DEEP_TIER_TOOLS)), result["detail"])
        self.assertIn("BERSERK_MCP_TIER", result["detail"])

    def test_registered_in_doctor_check_list(self):
        names = [name for name, _ in bm._DOCTOR_CHECK_FUNCS]
        self.assertIn("tool_tier", names)


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

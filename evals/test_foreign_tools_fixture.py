#!/usr/bin/env python3
"""Tests for evals/foreign_tools_fixture.py (issue #78): the fixture must
look like real OpenAI/Anthropic tool schemas and must never collide with a
real berserk-mcp tool name, or the multi-server eval would be testing
something other than what it claims to."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import foreign_tools_fixture  # noqa: E402
import berserk_mcp as bm  # noqa: E402


class ForeignToolsFixtureTest(unittest.TestCase):
    def test_at_least_a_few_tools_from_two_plausible_servers(self):
        names = [t["name"] for t in foreign_tools_fixture.FOREIGN_TOOLS]
        self.assertGreaterEqual(len(names), 6)
        self.assertTrue(any(n.startswith("slack_") for n in names))
        self.assertTrue(any(n.startswith("github_") for n in names))

    def test_no_name_collides_with_a_real_berserk_mcp_tool(self):
        foreign_names = {t["name"] for t in foreign_tools_fixture.FOREIGN_TOOLS}
        real_names = {t["name"] for t in bm.TOOLS}
        self.assertEqual(foreign_names & real_names, set())

    def test_every_tool_has_name_description_and_parameters(self):
        for t in foreign_tools_fixture.FOREIGN_TOOLS:
            self.assertIn("name", t)
            self.assertIn("description", t)
            self.assertTrue(t["description"])
            self.assertIn("parameters", t)
            self.assertEqual(t["parameters"].get("type"), "object")

    def test_openai_shape_matches_to_openai_tools_convention(self):
        wrapped = foreign_tools_fixture.to_openai_foreign_tools()
        self.assertEqual(len(wrapped), len(foreign_tools_fixture.FOREIGN_TOOLS))
        for entry in wrapped:
            self.assertEqual(entry["type"], "function")
            self.assertIn("name", entry["function"])
            self.assertIn("parameters", entry["function"])

    def test_anthropic_shape_matches_to_anthropic_tools_convention(self):
        wrapped = foreign_tools_fixture.to_anthropic_foreign_tools()
        self.assertEqual(len(wrapped), len(foreign_tools_fixture.FOREIGN_TOOLS))
        for entry in wrapped:
            self.assertIn("name", entry)
            self.assertIn("description", entry)
            self.assertIn("input_schema", entry)


if __name__ == "__main__":
    unittest.main()

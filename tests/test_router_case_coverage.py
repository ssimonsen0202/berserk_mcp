"""Every static tool must have a router eval case or a written reason why not."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import berserk_mcp  # noqa: E402

CASE_GLOB = "router_cases*.jsonl"

# tool name -> one-line reason it has no routing case. Keep this list short and honest;
# a tool that is hard to phrase a prompt for is usually a tool with an unclear description.
EXCLUDED = {}


def _load_cases():
    files = sorted((ROOT / "evals").glob(CASE_GLOB))
    cases = []
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                cases.append((path.name, lineno, json.loads(line)))
    return files, cases


class RouterCaseCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files, cls.cases = _load_cases()
        cls.tool_names = {t["name"] for t in berserk_mcp.TOOLS + berserk_mcp.MGMT_TOOLS}
        cls.covered = {case["expect_tool"] for _, _, case in cls.cases}

    def test_case_files_found(self):
        # Fail closed: a glob that matches nothing must not read as "zero gaps".
        names = {p.name for p in self.files}
        self.assertIn("router_cases.jsonl", names)
        self.assertIn("router_cases_extended.jsonl", names)
        self.assertGreater(len(self.cases), len(self.tool_names) // 2)

    def test_every_tool_has_a_case_or_exclusion(self):
        missing = sorted(self.tool_names - self.covered - set(EXCLUDED))
        self.assertEqual(
            missing,
            [],
            f"tools with no router eval case: {missing}. Add a case to "
            f"evals/router_cases_extended.jsonl, or add the tool to EXCLUDED with a reason.",
        )

    def test_exclusions_are_live_and_justified(self):
        for name, reason in EXCLUDED.items():
            self.assertIn(name, self.tool_names, f"EXCLUDED names unknown tool {name!r}")
            self.assertNotIn(name, self.covered, f"{name!r} now has a case; remove it from EXCLUDED")
            self.assertTrue(str(reason).strip(), f"EXCLUDED[{name!r}] needs a reason")

    def test_every_case_targets_a_real_tool(self):
        for filename, lineno, case in self.cases:
            with self.subTest(file=filename, line=lineno):
                self.assertIn(case.get("expect_tool"), self.tool_names)

    def test_case_ids_unique_across_files(self):
        seen = {}
        for filename, lineno, case in self.cases:
            case_id = case.get("id")
            self.assertTrue(case_id, f"{filename}:{lineno} has no id")
            self.assertNotIn(case_id, seen, f"duplicate id {case_id!r} in {filename}:{lineno} and {seen.get(case_id)}")
            seen[case_id] = f"{filename}:{lineno}"

    def test_expected_args_exist_in_tool_schema(self):
        schemas = {t["name"]: t["inputSchema"] for t in berserk_mcp.TOOLS + berserk_mcp.MGMT_TOOLS}
        for filename, lineno, case in self.cases:
            props = schemas[case["expect_tool"]].get("properties", {})
            for arg, value in (case.get("expect_args") or {}).items():
                with self.subTest(file=filename, line=lineno, arg=arg):
                    self.assertIn(arg, props)
                    if "enum" in props[arg]:
                        self.assertIn(value, props[arg]["enum"])
                    if "pattern" in props[arg]:
                        self.assertRegex(str(value), props[arg]["pattern"])


if __name__ == "__main__":
    unittest.main()

"""Fail-closed tests for scripts/inspector_check.py's decision logic (no Node needed)."""

import importlib.util
import json
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inspector_check.py"
_spec = importlib.util.spec_from_file_location("inspector_check", _PATH)
inspector_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inspector_check)
evaluate = inspector_check.evaluate


def _report(n_tools=3, output_schema=True, findings=None):
    tools = [{"name": f"t{i}", "inputSchema": {"type": "object"}} for i in range(n_tools)]
    if output_schema and tools:
        tools[0]["outputSchema"] = {"type": "object"}
    report = {"result": {"tools": tools}}
    if findings is not None:
        report["schemaFindings"] = findings
    return json.dumps(report)


class InspectorCheckEvaluateTest(unittest.TestCase):
    def test_clean_report_passes_in_both_eras(self):
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                self.assertEqual(evaluate(era, 0, _report(), 3), [])

    def test_warning_findings_fail_even_with_exit_zero(self):
        self.assertTrue(evaluate("legacy", 0, _report(findings=[{"severity": "warning"}]), 3))

    def test_error_findings_exit_six_fails(self):
        self.assertTrue(evaluate("legacy", 6, _report(findings=[{"severity": "error"}]), 3))

    def test_nonzero_exit_fails(self):
        self.assertTrue(evaluate("legacy", 1, _report(), 3))

    def test_unparseable_or_empty_output_fails(self):
        for stdout in ("", "not json", "[]", None):
            with self.subTest(stdout=stdout):
                self.assertTrue(evaluate("legacy", 0, stdout, 3))

    def test_fewer_tools_than_static_set_fails(self):
        self.assertTrue(evaluate("legacy", 0, _report(n_tools=2), 3))
        self.assertTrue(evaluate("legacy", 0, _report(n_tools=0), 3))

    def test_modern_without_output_schema_means_era_not_negotiated(self):
        self.assertTrue(evaluate("modern", 0, _report(output_schema=False), 3))
        self.assertEqual(evaluate("legacy", 0, _report(output_schema=False), 3), [])


if __name__ == "__main__":
    unittest.main()

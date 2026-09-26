"""Fail-closed tests for scripts/tob_semgrep_gate.py's decision logic (no semgrep or network needed)."""

import contextlib
import importlib.util
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "tob_semgrep_gate.py"
_spec = importlib.util.spec_from_file_location("tob_semgrep_gate", _PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _report(results=(), errors=(), scanned=("README.md",)):
    return {"results": list(results), "errors": list(errors), "paths": {"scanned": list(scanned)}}


def _result(rule, path="docs/a.md", line=3):
    return {"check_id": f"tob.rules.{rule}", "path": path, "start": {"line": line}}


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class EvaluateTest(unittest.TestCase):
    def test_clean_report_passes(self):
        self.assertEqual(gate.evaluate(_report()), ([], None))

    def test_findings_are_reported(self):
        findings, error = gate.evaluate(_report([_result("curl-insecure")]))
        self.assertIsNone(error)
        self.assertEqual(findings, ["curl-insecure docs/a.md:3"])

    def test_semgrep_errors_fail_closed(self):
        _, error = gate.evaluate(_report(errors=[{"message": "rule parse error"}]))
        self.assertIn("error", error)

    def test_zero_scanned_files_fail_closed(self):
        _, error = gate.evaluate(_report(scanned=()))
        self.assertIn("zero files", error)

    def test_malformed_report_fails_closed(self):
        for report in (None, {}, {"results": "x"}):
            with self.subTest(report=report):
                self.assertIsNotNone(gate.evaluate(report)[1])

    def test_canary_expects_every_planted_rule(self):
        self.assertEqual(len(gate.CANARY_EXPECTED), 5)
        self.assertEqual(
            gate.CANARY_EXPECTED - gate.fired_rules(["curl-insecure x:1", "curl-unencrypted-url x:2"]),
            {"ssh-disable-host-key-checking", "wget-no-check-certificate", "tarfile-extractall-traversal"},
        )


class FetchAndRunTest(unittest.TestCase):
    def setUp(self):
        # An existing clone: fetch_rules skips `git clone` when .git is a directory.
        self._tmp = tempfile.TemporaryDirectory()
        self.clone = Path(self._tmp.name)
        (self.clone / ".git").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_head_other_than_the_pin_fails(self):
        with mock.patch.object(gate.subprocess, "run", return_value=_completed(stdout="0" * 40)):
            error = gate.fetch_rules(self.clone)
        self.assertIn("expected", error)

    def test_modified_rules_checkout_fails(self):
        runs = [_completed(), _completed(stdout=gate.PINNED_COMMIT + "\n"), _completed(stdout=" M generic/x.yaml\n")]
        with mock.patch.object(gate.subprocess, "run", side_effect=runs):
            error = gate.fetch_rules(self.clone)
        self.assertIn("not clean", error)

    def test_unscanned_tracked_files_are_reported(self):
        def entry(mode, path):
            return f"{mode} {'0' * 40} 0\t{path}\0"

        listing = _completed(
            stdout=entry("100644", "README.md")
            + entry("100755", "tests/a.py")
            + entry("100644", "docs/caf\u00e9 notes.md")
            + entry("120000", "link.md")
            + entry("160000", "vendor/sub")
            + entry("100644", gate.SELF_EXCLUDE)
        )
        with mock.patch.object(gate.subprocess, "run", return_value=listing):
            self.assertEqual(
                gate.unscanned_tracked_files(".", ["README.md", "docs/caf\u00e9 notes.md"]), ["tests/a.py"]
            )
        with mock.patch.object(gate.subprocess, "run", return_value=_completed(returncode=128)):
            self.assertIsNone(gate.unscanned_tracked_files(".", []))

    def test_clone_failure_fails(self):
        with mock.patch.object(gate.subprocess, "run", return_value=_completed(returncode=128, stderr="denied")):
            self.assertIn("clone failed", gate.fetch_rules("/nonexistent/tob"))

    def test_semgrep_nonzero_exit_and_bad_json_fail(self):
        with mock.patch.object(gate.subprocess, "run", return_value=_completed(returncode=2, stderr="boom")):
            self.assertIn("exited 2", gate.run_semgrep("/r", ".")[1])
        with mock.patch.object(gate.subprocess, "run", return_value=_completed(stdout="not json")):
            self.assertIn("not JSON", gate.run_semgrep("/r", ".")[1])

    def test_main_fails_when_canary_rules_do_not_fire(self):
        with mock.patch.object(gate, "fetch_rules", return_value=None):
            with mock.patch.object(gate, "run_semgrep", return_value=(_report([_result("curl-insecure")]), None)):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(gate.main(["--rules-dir", "/r"]), 1)
        self.assertIn("canary check", out.getvalue())

    def test_main_fails_when_tracked_files_were_skipped(self):
        canary = _report([_result(rule) for rule in gate.CANARY_EXPECTED])
        with mock.patch.object(gate, "fetch_rules", return_value=None):
            with mock.patch.object(gate, "run_semgrep", side_effect=[(canary, None), (_report(), None)]):
                with mock.patch.object(gate, "unscanned_tracked_files", return_value=["tests/a.py"]):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(gate.main(["--rules-dir", "/r"]), 1)
        self.assertIn("tests/a.py", out.getvalue())

    def test_main_passes_only_with_canary_hits_and_a_clean_repo(self):
        canary = _report([_result(rule) for rule in gate.CANARY_EXPECTED])
        with mock.patch.object(gate, "fetch_rules", return_value=None):
            with mock.patch.object(gate, "run_semgrep", side_effect=[(canary, None), (_report(), None)]) as run:
                with mock.patch.object(gate, "unscanned_tracked_files", return_value=[]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(gate.main(["--rules-dir", "/r"]), 0)
        self.assertEqual(run.call_args_list[1].kwargs["exclude"], (gate.SELF_EXCLUDE,))


if __name__ == "__main__":
    unittest.main()

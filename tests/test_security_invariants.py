import ast
import fnmatch
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ARTIFACT_PATTERNS = (
    "README.md", "CONTRIBUTING.md", "SECURITY.md",
    "primers/*.md", "primers/**/*.md",
    "docs/*.md", "docs/**/*.md",
    "dashboards/*.md", "dashboards/**/*.md", "dashboards/**/*.json",
    "dashboards/**/*.kql",
    "evals/*.md", "evals/**/*.md",
    "ingestion_catalog.json", "pricing_catalog.json",
)
PRIVATE_DEPLOYMENT_MARKERS = (
    "HermesRuntime", "OpenClaw", "homelab", "hermes-discord",
    "check-esxi-snap", "ssn-bzrk", "/opt/assistant", "/home/assistant",
    "192.168.",
)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)


class SecurityInvariantTest(unittest.TestCase):
    def _violations(self, source, filename="<source>"):
        tree = ast.parse(source, filename=filename)
        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"eval", "exec", "compile"}:
                violations.append((node.lineno, f"forbidden builtin {func.id}"))
            if isinstance(func, ast.Attribute):
                owner = func.value.id if isinstance(func.value, ast.Name) else ""
                if owner == "os" and func.attr in {"system", "popen"}:
                    violations.append((node.lineno, f"forbidden os.{func.attr}"))
                if owner == "subprocess" and func.attr in {"run", "Popen", "call", "check_call", "check_output"}:
                    for keyword in node.keywords:
                        if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                            violations.append((node.lineno, "subprocess shell=True"))
                    if node.args and isinstance(node.args[0], (ast.Constant, ast.JoinedStr, ast.BinOp)):
                        violations.append((node.lineno, "subprocess argv is a string expression"))
        return violations

    def test_repository_has_no_shell_or_dynamic_execution(self):
        violations = []
        tracked = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
            text=True, check=True,
        ).stdout.splitlines()
        for relative in sorted(tracked):
            path = ROOT / relative
            for line, message in self._violations(path.read_text(encoding="utf-8"), str(path)):
                violations.append(f"{path.relative_to(ROOT)}:{line}: {message}")
        self.assertEqual(violations, [])

    def test_scanner_detects_unsafe_fixture(self):
        fixture = "import subprocess\nsubprocess.run('echo unsafe', shell=True)\n"
        messages = [message for _, message in self._violations(fixture)]
        self.assertIn("subprocess shell=True", messages)
        self.assertIn("subprocess argv is a string expression", messages)

    def _public_artifact_leaks(self, relative, text):
        leaks = [marker for marker in PRIVATE_DEPLOYMENT_MARKERS if marker in text]
        leaks.extend(match.group(0) for match in UUID_RE.finditer(text))
        return [f"{relative}: {leak}" for leak in leaks]

    def test_tracked_public_artifacts_contain_no_private_deployment_inventory(self):
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        covered = sorted(
            relative for relative in tracked
            if any(fnmatch.fnmatch(relative, pattern) for pattern in PUBLIC_ARTIFACT_PATTERNS)
        )
        self.assertIn("README.md", covered)
        self.assertTrue(any(path.startswith("docs/") for path in covered))
        self.assertTrue(any(path.startswith("dashboards/") for path in covered))
        self.assertTrue(any(path.startswith("evals/") for path in covered))

        leaks = []
        for relative in covered:
            text = (ROOT / relative).read_text(encoding="utf-8")
            leaks.extend(self._public_artifact_leaks(relative, text))
        self.assertEqual(leaks, [])

    def test_public_artifact_leak_guard_detects_markers_and_live_uuid(self):
        fixture = (
            "profile=homelab host=HermesRuntime session="
            "1775e12f-d0ea-4edd-a690-0578e90d5efe"
        )
        leaks = self._public_artifact_leaks("fixture.md", fixture)
        self.assertTrue(any("homelab" in leak for leak in leaks))
        self.assertTrue(any("HermesRuntime" in leak for leak in leaks))
        self.assertTrue(any("1775e12f" in leak for leak in leaks))


if __name__ == "__main__":
    unittest.main()

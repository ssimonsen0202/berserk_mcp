import ast
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent


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


if __name__ == "__main__":
    unittest.main()

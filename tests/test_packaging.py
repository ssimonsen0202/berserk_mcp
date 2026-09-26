"""The installed package must ship every local module the server can import.

pyproject.toml lists modules explicitly (py-modules). A module imported by the
server but missing from that list works in a source checkout and fails only
after `pip install`, often only when one tool runs (a function-level import).
"""

import ast
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def reachable_local_modules(entries):
    """Local top-level modules reachable by imports at any depth, from `entries`."""
    local = {p.stem for p in ROOT.glob("*.py")}
    needs, seen, todo = {e: {"[project.scripts]"} for e in entries}, set(), list(entries)
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        for node in ast.walk(ast.parse((ROOT / f"{mod}.py").read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name in local:
                    needs.setdefault(name, set()).add(mod)
                    todo.append(name)
    return needs


def _pyproject():
    return (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def listed_py_modules():
    return set(tomllib.loads(_pyproject())["tool"]["setuptools"]["py-modules"])


def script_entry_modules():
    """Modules named by [project.scripts] entries such as `x = "module:func"`."""
    scripts = tomllib.loads(_pyproject())["project"].get("scripts", {})
    return {target.split(":")[0] for target in scripts.values()}


class PackagingTest(unittest.TestCase):
    def test_entry_points_found(self):
        # Fail closed: an empty entry set would make the reachability check vacuous.
        self.assertIn("berserk_mcp", script_entry_modules())

    def test_every_reachable_local_module_is_packaged(self):
        needs = reachable_local_modules(script_entry_modules())
        missing = {m: sorted(by) for m, by in needs.items() if m not in listed_py_modules()}
        self.assertEqual(missing, {}, f"add to [tool.setuptools] py-modules in pyproject.toml: {missing}")

    def test_listed_modules_exist(self):
        for mod in listed_py_modules():
            with self.subTest(module=mod):
                self.assertTrue((ROOT / f"{mod}.py").is_file(), f"py-modules lists missing file {mod}.py")

    def test_typechecked_modules_match_shipped_modules(self):
        # `make typecheck` (a CI step) must cover exactly what ships, so a new
        # module cannot be packaged without being type-checked.
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        match = re.search(r"^TYPED_MODULES\s*=\s*(.+)$", makefile, re.MULTILINE)
        self.assertIsNotNone(match, "TYPED_MODULES not found in Makefile")
        typed = {name[: -len(".py")] for name in match.group(1).split() if name.endswith(".py")}
        self.assertEqual(typed, listed_py_modules())


if __name__ == "__main__":
    unittest.main()

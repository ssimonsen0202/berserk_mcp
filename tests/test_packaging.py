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


def _local_packages():
    return {p.parent.name for p in ROOT.glob("*/__init__.py") if p.parent.name != "tests"}


def _module_sources(mod):
    """Source files of a local module. A package wins over a same-named
    root file on import (berserk_mcp.py is only a launcher), so a package
    means every .py file under it."""
    if (ROOT / mod / "__init__.py").is_file():
        return sorted((ROOT / mod).rglob("*.py"))
    return [ROOT / f"{mod}.py"]


def reachable_local_modules(entries):
    """Local top-level modules reachable by imports at any depth, from `entries`."""
    local = {p.stem for p in ROOT.glob("*.py")} | _local_packages()
    needs, seen, todo = {e: {"[project.scripts]"} for e in entries}, set(), list(entries)
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        trees = [ast.parse(path.read_text(encoding="utf-8")) for path in _module_sources(mod)]
        for node in (n for tree in trees for n in ast.walk(tree)):
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


def listed_packages():
    return set(tomllib.loads(_pyproject())["tool"]["setuptools"].get("packages", []))


def listed_top_level():
    """Everything that ships at the top level: py-modules and top-level packages."""
    return listed_py_modules() | {p for p in listed_packages() if "." not in p}


def script_entry_modules():
    """Modules named by [project.scripts] entries such as `x = "module:func"`."""
    scripts = tomllib.loads(_pyproject())["project"].get("scripts", {})
    return {target.split(":")[0].split(".")[0] for target in scripts.values()}


class PackagingTest(unittest.TestCase):
    def test_entry_points_found(self):
        # Fail closed: an empty entry set would make the reachability check vacuous.
        self.assertIn("berserk_mcp", script_entry_modules())

    def test_every_reachable_local_module_is_packaged(self):
        needs = reachable_local_modules(script_entry_modules())
        missing = {m: sorted(by) for m, by in needs.items() if m not in listed_top_level()}
        self.assertEqual(missing, {}, f"add to [tool.setuptools] py-modules or packages in pyproject.toml: {missing}")

    def test_entry_package_is_actually_scanned(self):
        # Fail closed: the root launcher berserk_mcp.py imports almost nothing,
        # so reachability must follow the package, not the launcher.
        needs = reachable_local_modules(script_entry_modules())
        self.assertIn("ai_finops", needs)
        self.assertIn("_kql_boundary", needs)

    def test_every_subpackage_is_listed(self):
        for init in ROOT.glob("berserk_mcp/**/__init__.py"):
            package = ".".join(init.parent.relative_to(ROOT).parts)
            with self.subTest(package=package):
                self.assertIn(package, listed_packages())

    def test_launcher_is_not_shipped(self):
        # berserk_mcp.py at the root only starts the server from a checkout.
        self.assertNotIn("berserk_mcp", listed_py_modules())

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
        entries = match.group(1).split()
        typed_modules = {name.removesuffix(".py") for name in entries if name.endswith(".py")}
        typed_packages = {name.rstrip("/") for name in entries if not name.endswith(".py")}
        # A .py entry checks a single file; a bare entry checks a package
        # directory. `berserk_mcp.py` (the launcher) must not stand in for
        # the berserk_mcp/ package.
        self.assertEqual(typed_modules, listed_py_modules())
        self.assertEqual(typed_packages, {p for p in listed_packages() if "." not in p})

    def test_listed_packages_exist(self):
        packages = listed_packages()
        self.assertTrue(packages, "[tool.setuptools] packages is empty")
        for package in packages:
            with self.subTest(package=package):
                init = ROOT.joinpath(*package.split(".")) / "__init__.py"
                self.assertTrue(init.is_file(), f"packages lists {package}, but {init} is missing")


if __name__ == "__main__":
    unittest.main()

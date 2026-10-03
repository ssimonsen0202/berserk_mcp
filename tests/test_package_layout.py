"""The berserk_mcp package keeps its public surface and its layering.

berserk_mcp.py was split into the berserk_mcp/ package in v1.37.0. Tests,
scripts and evals read and patch names on `berserk_mcp`; the facade
(berserk_mcp/_facade.py) routes those reads and writes to the owning module.
"""

import ast
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402
from berserk_mcp import _facade  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "berserk_mcp"
NAMES = Path(__file__).resolve().parent / "facade_names.txt"
MAX_MODULE_LINES = 1200
MAX_INIT_LINES = 80  # __init__.py is only the facade
# No module is exempt; a missing LAYERS file fails package import first.
SIZE_EXEMPT: set[str] = set()


def package_modules():
    """(dotted layer name, path) for every module under berserk_mcp/."""
    out = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        out.append((".".join(parts), path))
    return out


def package_imports(path):
    """(lineno, module, imported name) for each `from berserk_mcp... import x`."""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").startswith("berserk_mcp"):
            for alias in node.names:
                out.append((node.lineno, node.module, alias.name))
    return out


def relative_imports(path):
    """Line numbers of relative imports (`from . import x`, `from .m import x`)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]


class FacadeSurfaceTest(unittest.TestCase):
    def test_every_name_tests_and_scripts_use_still_resolves(self):
        names = NAMES.read_text(encoding="utf-8").split()
        self.assertGreater(len(names), 200)  # fail closed on an empty snapshot
        missing = [n for n in names if not hasattr(bm, n)]
        self.assertEqual(missing, [])

    def test_version_is_exposed(self):
        self.assertRegex(bm.__version__, r"^\d+\.\d+\.\d+$")


class FacadeRoutingTest(unittest.TestCase):
    """The facade on throwaway modules, so it is tested apart from the server."""

    def setUp(self):
        import types

        self.pkg = types.ModuleType("fakepkg")
        self.owner = types.ModuleType("fakepkg.config")
        self.owner.LIMIT = 10
        sys.modules["fakepkg"] = self.pkg
        sys.modules["fakepkg.config"] = self.owner
        self.pkg.__class__ = _facade.Facade
        self.addCleanup(sys.modules.pop, "fakepkg", None)
        self.addCleanup(sys.modules.pop, "fakepkg.config", None)

    def test_read_comes_from_the_owner(self):
        self.assertEqual(self.pkg.LIMIT, 10)
        self.owner.LIMIT = 11
        self.assertEqual(self.pkg.LIMIT, 11)  # never a stale copy

    def test_write_lands_in_the_owner(self):
        self.pkg.LIMIT = 5
        self.assertEqual(self.owner.LIMIT, 5)
        self.assertNotIn("LIMIT", self.pkg.__dict__)

    def test_patch_object_restores_the_owner(self):
        # mock restores a non-local attribute by delete-then-set.
        with mock.patch.object(self.pkg, "LIMIT", 99):
            self.assertEqual(self.owner.LIMIT, 99)
        self.assertEqual(self.owner.LIMIT, 10)
        self.assertNotIn("LIMIT", self.pkg.__dict__)

    def test_owner_memory_is_per_package(self):
        with mock.patch.object(self.pkg, "LIMIT", 99):
            pass
        # The real package must not resolve a name through the fake one.
        with self.assertRaises(AttributeError):
            _ = bm.LIMIT
        self.assertIsNone(_facade.owner_of(bm, "LIMIT"))

    def test_unknown_name_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            _ = self.pkg.NOPE

    def test_new_name_is_set_on_the_package(self):
        self.pkg.FRESH = 1
        self.assertEqual(self.pkg.__dict__["FRESH"], 1)


class FacadeInstallTest(unittest.TestCase):
    """install() skips a layer whose parent package is absent, and nothing else."""

    PKG_NAME = "fakeinstallpkg"

    def make_package(self, handlers_init=None):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / self.PKG_NAME).mkdir()
        (root / self.PKG_NAME / "__init__.py").write_text("", encoding="utf-8")
        if handlers_init is not None:
            (root / self.PKG_NAME / "handlers").mkdir()
            (root / self.PKG_NAME / "handlers" / "__init__.py").write_text(handlers_init, encoding="utf-8")
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.addCleanup(self.forget_package)
        return importlib.import_module(self.PKG_NAME)

    def forget_package(self):
        for name in [n for n in sys.modules if n == self.PKG_NAME or n.startswith(self.PKG_NAME + ".")]:
            del sys.modules[name]

    def test_broken_handlers_package_raises(self):
        package = self.make_package(handlers_init="import nonexistent_dependency_for_test\n")
        with self.assertRaises(ModuleNotFoundError) as caught:
            _facade.install(package)
        self.assertEqual(caught.exception.name, "nonexistent_dependency_for_test")

    def test_absent_handlers_package_is_skipped(self):
        package = self.make_package()
        _facade.install(package)
        self.assertIsInstance(package, _facade.Facade)


class LayeringTest(unittest.TestCase):
    def test_every_module_is_a_known_layer(self):
        known = set(_facade.LAYERS) | {"", "_facade", "__main__", "handlers"}
        for layer, path in package_modules():
            with self.subTest(module=str(path)):
                self.assertIn(layer, known)

    def test_every_layer_has_a_module(self):
        # A LAYERS entry with no file would make install() skip or fail it silently.
        for layer in _facade.LAYERS:
            path = PKG.joinpath(*layer.split(".")).with_suffix(".py")
            with self.subTest(layer=layer):
                self.assertTrue(path.is_file(), f"LAYERS names {layer} but {path} is missing")

    def test_modules_import_only_lower_layers(self):
        rank = {layer: i for i, layer in enumerate(_facade.LAYERS)}
        for layer, path in package_modules():
            if layer not in rank:
                continue
            for lineno, module, name in package_imports(path):
                target = (module.removeprefix("berserk_mcp").lstrip(".") + "." + name).lstrip(".")
                if target in ("_version.__version__", "_facade"):
                    continue
                with self.subTest(module=str(path), line=lineno):
                    self.assertIn(
                        target,
                        rank,
                        f"{path.name}:{lineno} imports {target}, not a layer",
                    )
                    self.assertLess(
                        rank[target],
                        rank[layer],
                        f"{path.name}:{lineno} imports upward: {target}",
                    )

    def test_package_modules_are_imported_whole_with_a_bm_alias(self):
        # `from berserk_mcp.config import LIMIT` copies the value; a test that
        # patches berserk_mcp.LIMIT would then miss this reader.
        for layer, path in package_modules():
            if layer == "__main__":  # the entry point imports main itself
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("berserk_mcp"):
                    for alias in node.names:
                        if alias.name in ("__version__", "_facade"):
                            continue
                        with self.subTest(module=str(path), line=node.lineno):
                            self.assertTrue(
                                (alias.asname or "").startswith("bm_"),
                                f"{path.name}:{node.lineno}: import a package module as bm_<name>",
                            )

    def test_no_relative_imports(self):
        # `from .config import LIMIT` escapes both tests above, which only
        # see absolute `berserk_mcp` imports.
        for _, path in package_modules():
            with self.subTest(module=str(path)):
                self.assertEqual(
                    relative_imports(path), [], f"{path.name}: use `from berserk_mcp import <m> as bm_<m>`"
                )

    def test_relative_import_check_detects_relative_imports(self):
        # Fail closed: the check above must see a relative import when there is one.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.py"
            path.write_text("from .config import LIMIT\nfrom . import config\n", encoding="utf-8")
            self.assertEqual(relative_imports(path), [1, 2])


class CiCoverageTest(unittest.TestCase):
    def test_semgrep_scans_the_whole_package(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertRegex(ci, r"semgrep --config \.semgrep/ --error berserk_mcp/")

    def test_semgrep_rule_test_runs_in_ci(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "semgrep --test --config .semgrep/fence-untrusted-data.yml .semgrep/fence-untrusted-data.py",
            ci,
        )


class ModuleSizeTest(unittest.TestCase):
    def test_no_module_is_too_long(self):
        for _layer, path in package_modules():
            if path.name in SIZE_EXEMPT:
                continue
            with self.subTest(module=str(path)):
                count = len(path.read_text(encoding="utf-8").splitlines())
                self.assertLessEqual(count, MAX_MODULE_LINES, f"{path.name} has {count} lines")


class FacadeOnlyInitTest(unittest.TestCase):
    def test_init_is_only_the_facade(self):
        lines = (PKG / "__init__.py").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), MAX_INIT_LINES)


if __name__ == "__main__":
    unittest.main()

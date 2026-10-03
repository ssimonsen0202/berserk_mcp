"""scripts/split_move.py, the one-off tool that splits berserk_mcp/__init__.py.

Deleted with the tool when the split is done (v1.37.0)."""

import ast
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import split_move as sm  # noqa: E402

SOURCE = '''"""doc"""

import os

from berserk_mcp import _facade

LIMIT = int(os.environ.get("LIMIT", "3"))


def helper():
    return LIMIT + 1


def reader():
    return LIMIT * 2


_facade.install(None)
'''


class SplitMoveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        pkg = Path(self._tmp.name) / "berserk_mcp"
        pkg.mkdir()
        patcher = mock.patch.object(sm, "PKG", pkg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_moves_owned_code_and_rewrites_remaining_readers(self):
        owners = {"LIMIT": "config", "helper": "config", "reader": "runner"}
        plan = sm.plan_move("config", owners, SOURCE)
        module = sm.render_module("config", plan)
        init = sm.render_init("config", plan)
        self.assertIn("import os", module)
        self.assertIn("def helper():\n    return LIMIT + 1", module)
        self.assertIn("return bm_config.LIMIT * 2", init)
        self.assertIn("from berserk_mcp import config as bm_config", init)
        self.assertNotIn("import os", init)  # nothing left reads it
        self.assertIn("_facade.install(None)", init)  # the facade call never moves
        ast.parse(module)
        ast.parse(init)

    def test_facade_install_stays_when_the_last_module_moves(self):
        owners = {"LIMIT": "cli", "helper": "cli", "reader": "cli"}
        init = sm.render_init("cli", sm.plan_move("cli", owners, SOURCE))
        self.assertIn("_facade.install(None)", init)
        self.assertNotIn("def reader", init)

    def test_kept_version_import_becomes_an_explicit_re_export(self):
        source = SOURCE.replace(
            "from berserk_mcp import _facade\n",
            "from berserk_mcp import _facade\nfrom berserk_mcp._version import __version__\n",
        )
        owners = {"LIMIT": "cli", "helper": "cli", "reader": "cli"}
        init = sm.render_init("cli", sm.plan_move("cli", owners, source))
        self.assertIn("from berserk_mcp._version import __version__ as __version__", init)

    def test_refuses_an_upward_reference(self):
        owners = {"LIMIT": "runner", "helper": "config", "reader": "config"}
        with self.assertRaisesRegex(sm.SplitError, "owned by runner .not moved yet."):
            sm.plan_move("config", owners, SOURCE)

    def test_refuses_a_global_write_to_a_moved_name(self):
        source = SOURCE + "\n\ndef bump():\n    global LIMIT\n    LIMIT = 9\n"
        owners = {"LIMIT": "config", "helper": "config", "reader": "runner", "bump": "runner"}
        with self.assertRaisesRegex(sm.SplitError, "global LIMIT"):
            sm.plan_move("config", owners, source)

    def test_refuses_a_local_that_shadows_a_rewritten_name(self):
        source = SOURCE + "\n\ndef shadow():\n    LIMIT = 1\n    return LIMIT\n"
        owners = {"LIMIT": "config", "helper": "config", "reader": "runner", "shadow": "runner"}
        with self.assertRaisesRegex(sm.SplitError, "shadow"):
            sm.plan_move("config", owners, source)

    def test_refuses_a_name_without_an_owner(self):
        with self.assertRaisesRegex(sm.SplitError, "no owner"):
            sm.plan_move("config", {"LIMIT": "config", "helper": "config"}, SOURCE)

    def test_function_shape_ignores_only_module_prefixes(self):
        bare = "def f():\n    return LIMIT + g(x)\n"
        prefixed = "def f():\n    return bm_config.LIMIT + bm_runner.g(\n        x,\n    )\n"
        changed = "def f():\n    return bm_config.LIMIT - bm_runner.g(x)\n"
        self.assertEqual(sm._function_shape(bare, "f"), sm._function_shape(prefixed, "f"))
        self.assertNotEqual(sm._function_shape(bare, "f"), sm._function_shape(changed, "f"))


if __name__ == "__main__":
    unittest.main()

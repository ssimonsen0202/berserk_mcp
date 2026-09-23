"""Security-critical functions carry a review fingerprint that expires on change.

tests/security_reviews.json maps "module:function" to the fingerprint of the
function's code when it was last reviewed. Editing the code changes the
fingerprint and fails this test until someone re-reviews it and records the new
value with a note. Comments, the docstring and blank lines are not part of the
fingerprint, so explaining code does not expire a review; changing it does.

Print current fingerprints:  python3 tests/test_security_reviews.py --print
"""

import ast
import hashlib
import io
import json
import sys
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = Path(__file__).resolve().parent / "security_reviews.json"


def _function_node(tree, name):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise LookupError(name)


def fingerprint_source(source, name):
    """Fingerprint top-level function `name` in `source`.

    Text-based, not ast.dump-based: ast.dump output differs across the Python
    versions CI runs (3.9-3.12, and 3.13+ omits None fields), which would make
    the fingerprint version-dependent.
    """
    node = _function_node(ast.parse(source), name)
    lines = source.splitlines()
    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
    segment = lines[start - 1 : node.end_lineno]
    drop = set()
    first = node.body[0] if node.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        drop = set(range(first.lineno - start, first.end_lineno - start + 1))
    comment_cols = {}
    for tok in tokenize.generate_tokens(io.StringIO("\n".join(segment) + "\n").readline):
        if tok.type == tokenize.COMMENT:
            comment_cols[tok.start[0] - 1] = tok.start[1]
    kept = []
    for i, line in enumerate(segment):
        if i in drop:
            continue
        if i in comment_cols:
            line = line[: comment_cols[i]]
        line = line.rstrip()
        if line:
            kept.append(line)
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()[:16]


def fingerprint(key):
    module, name = key.split(":")
    return fingerprint_source((ROOT / f"{module}.py").read_text(encoding="utf-8"), name)


def load_registry():
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


class SecurityReviewFingerprintTest(unittest.TestCase):
    def test_registry_is_populated(self):
        self.assertGreaterEqual(len(load_registry()), 6)

    def test_every_entry_is_justified(self):
        for key, entry in load_registry().items():
            with self.subTest(key=key):
                for field in ("fingerprint", "reviewed", "by", "note"):
                    self.assertTrue(str(entry.get(field, "")).strip(), f"{key} needs {field}")

    def test_reviewed_code_has_not_changed_since_review(self):
        expired = []
        for key, entry in load_registry().items():
            current = fingerprint(key)
            if current != entry["fingerprint"]:
                expired.append(f"{key} changed since its review on {entry['reviewed']} (new fingerprint {current})")
        self.assertEqual(
            expired,
            [],
            "Re-review these functions, then record the new fingerprint, date, reviewer and a note "
            "in tests/security_reviews.json:\n" + "\n".join(expired),
        )


class FingerprintSourceTest(unittest.TestCase):
    BASE = 'def f(x):\n    """Doc."""\n    y = x + 1  # add one\n    return y\n'

    def test_comments_docstring_and_blank_lines_do_not_count(self):
        reworded = 'def f(x):\n    """Different doc,\n    two lines."""\n\n    y = x + 1  # a new comment\n    # more\n    return y\n'
        self.assertEqual(fingerprint_source(self.BASE, "f"), fingerprint_source(reworded, "f"))

    def test_code_change_changes_fingerprint(self):
        changed = self.BASE.replace("x + 1", "x + 2")
        self.assertNotEqual(fingerprint_source(self.BASE, "f"), fingerprint_source(changed, "f"))

    def test_hash_inside_string_is_code_not_comment(self):
        a = 'def f():\n    return "a # b"\n'
        b = 'def f():\n    return "a # c"\n'
        self.assertNotEqual(fingerprint_source(a, "f"), fingerprint_source(b, "f"))

    def test_decorator_change_counts(self):
        a = "@dec\ndef f():\n    return 1\n"
        b = "@other\ndef f():\n    return 1\n"
        self.assertNotEqual(fingerprint_source(a, "f"), fingerprint_source(b, "f"))

    def test_missing_function_raises(self):
        with self.assertRaises(LookupError):
            fingerprint_source(self.BASE, "g")


if __name__ == "__main__":
    if "--print" in sys.argv:
        for key in load_registry():
            print(key, fingerprint(key))
    else:
        unittest.main()

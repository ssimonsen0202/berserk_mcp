"""Every behavioural section of SECURITY.md must be cited by at least one test.

Tests cite a section with a comment `# Covers SECURITY.md#<slug>`, where the slug
is the `##` heading lower-cased with runs of non-alphanumerics turned into `-`.
A new section with no test, or a citation to a renamed heading, fails here.
"""

import io
import re
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECURITY_MD = ROOT / "SECURITY.md"
CITATION_RE = re.compile(r"SECURITY\.md#([a-z0-9-]+)")

# section slug -> why no test is expected. Keep this list short.
EXEMPT = {
    "reporting-a-vulnerability": "reporting process, not code behaviour",
    "test-expectations": "how to run the tests, not a behaviour to test",
}


def slugify(heading):
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


def sections():
    text = SECURITY_MD.read_text(encoding="utf-8")
    return [slugify(m.group(1)) for m in re.finditer(r"^## (.+?)\s*$", text, re.MULTILINE)]


TEST_DEF_RE = re.compile(r"\s*(async\s+)?def test_\w*\s*\(")


def _cites_a_test(lines, comment_row):
    """True when the next code line after the comment (skipping further comments
    and decorators) defines a test function."""
    for line in lines[comment_row:]:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "@")):
            continue
        return bool(TEST_DEF_RE.match(line))
    return False


def citations():
    """Citations count only as real comment tokens in test modules, directly
    above a test function; strings, docstrings and helper modules do not."""
    found = {}
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.COMMENT:
                continue
            row = tok.start[0]
            for slug in CITATION_RE.findall(tok.string):
                if _cites_a_test(lines, row):
                    found.setdefault(slug, []).append(f"{path.name}:{row}")
    return found


class SecurityDocCoverageTest(unittest.TestCase):
    def test_sections_found(self):
        # Fail closed: a parse that finds nothing must not read as "all covered".
        self.assertGreaterEqual(len(sections()), 5)

    def test_every_behavioural_section_is_cited_by_a_test(self):
        cited = citations()
        missing = [s for s in sections() if s not in EXEMPT and s not in cited]
        self.assertEqual(missing, [], f"SECURITY.md sections with no test citing them: {missing}")

    def test_every_citation_points_at_an_existing_section(self):
        known = set(sections())
        stale = {slug: where for slug, where in citations().items() if slug not in known}
        self.assertEqual(stale, {}, f"citations to missing SECURITY.md sections: {stale}")

    def test_only_comments_above_test_functions_count(self):
        src = (
            '"""# Covers SECURITY.md#in-docstring"""\n'
            's = "# Covers SECURITY.md#in-string"\n'
            "# Covers SECURITY.md#above-helper\n"
            "def helper():\n    pass\n"
            "# Covers SECURITY.md#above-test\n"
            "@decorator\n"
            "def test_real():\n    pass\n"
        )
        lines = src.splitlines()
        counted = set()
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT and _cites_a_test(lines, tok.start[0]):
                counted.update(CITATION_RE.findall(tok.string))
        self.assertEqual(counted, {"above-test"})

    def test_exemptions_are_live_and_justified(self):
        known = set(sections())
        for slug, reason in EXEMPT.items():
            self.assertIn(slug, known, f"EXEMPT names a section that no longer exists: {slug}")
            self.assertTrue(reason.strip())


if __name__ == "__main__":
    unittest.main()

"""README prose follows Orwell's rules and ASD-STE100 Simplified Technical
English (STE), checked on every CI run.

What the checks look at: README.md prose only. Code blocks, tables, headings,
HTML, link targets, and inline code are removed first, so commands and
identifiers are never judged as prose.

The rules:
- No sentence longer than 25 words (the STE limit for descriptive text).
- No filler or stock phrase from STOCK_PHRASES (Orwell rules 1-3 and 5).
- Likely passive constructions stay at or below PASSIVE_LIMIT (Orwell rule 4).
  The passive check is a heuristic, so it is a ratchet, not a ban: lower the
  limit when you remove passives, and never raise it to fit new text.

To fix a failure, rewrite the sentence; the message gives its line number.
"""

import re
import unittest
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"
MAX_SENTENCE_WORDS = 25
PASSIVE_LIMIT = 49
STOCK_PHRASES = (
    "just",
    "very",
    "really",
    "simply",
    "basically",
    "actually",
    "in order to",
    "leverage",
    "utilize",
    "first-class",
    "out of the box",
    "under the hood",
    "robust",
    "seamless",
    "seamlessly",
    "a number of",
    "due to the fact",
    "it should be noted",
    "going forward",
    "best-in-class",
    "cutting-edge",
)
_PASSIVE_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being)\s+(?:\w+ed|built|done|made|shown|kept|sent|set|run|cut|"
    r"found|given|known|taken|written)\b",
    re.IGNORECASE,
)


def _phrase_re(phrase):
    # A hyphen or "#" on either side means a compound or an anchor, such as
    # "just-in-time", not the filler word.
    return re.compile(rf"(?<![-#\w]){re.escape(phrase)}(?![-\w])", re.IGNORECASE)


def prose_lines(text):
    """(line_number, prose) pairs with markup removed."""
    out = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith(("#", "|", "<", "[!")):
            continue
        line = re.sub(r"`[^`]*`", "CODE", line)  # inline code is not prose
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)  # keep link text only
        line = re.sub(r"^\s*(?:[-*>]|\d+\.)\s+", "", line)  # list and quote markers
        line = line.replace("**", "").replace("*", "")
        out.append((number, line.strip()))
    return out


def sentences(text):
    """(first_line, sentence) pairs. Paragraphs end at blank lines, list items,
    and headings, so a sentence never joins two separate items."""
    result, current, start = [], [], None
    previous = None
    for number, line in prose_lines(text):
        raw = text.splitlines()[number - 1].lstrip()
        new_block = previous is None or number != previous + 1 or re.match(r"(?:[-*>]|\d+\.)\s", raw)
        if new_block and current:
            result.extend(_split(" ".join(current), start))
            current = []
        if not current:
            start = number
        current.append(line)
        previous = number
    if current:
        result.extend(_split(" ".join(current), start))
    return result


_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "approx.", "cf.", "no.")


def _split(paragraph, start):
    # A sentence ends at ".", "!" or "?" plus a space, whatever case follows,
    # because sentences here often start with a lowercase name (berserk-mcp).
    parts, current = [], []
    for word in paragraph.split():
        current.append(word)
        if word[-1:] in ".!?" and word.lower() not in _ABBREVIATIONS:
            parts.append(" ".join(current))
            current = []
    if current:
        parts.append(" ".join(current))
    return [(start, p) for p in parts]


class ReadmeStyleTest(unittest.TestCase):
    def setUp(self):
        self.text = README.read_text(encoding="utf-8")

    def test_no_sentence_is_longer_than_the_ste_limit(self):
        long = [
            f"line {line}: {len(s.split())} words: {s[:90]}"
            for line, s in sentences(self.text)
            if len(s.split()) > MAX_SENTENCE_WORDS
        ]
        self.assertEqual(long, [], f"{len(long)} sentences over {MAX_SENTENCE_WORDS} words")

    def test_no_filler_or_stock_phrases(self):
        found = [
            f"line {number}: '{phrase}' in: {line[:90]}"
            for number, line in prose_lines(self.text)
            for phrase in STOCK_PHRASES
            if _phrase_re(phrase).search(line)
        ]
        self.assertEqual(found, [])

    def test_passive_voice_does_not_grow(self):
        prose = " ".join(line for _, line in prose_lines(self.text))
        count = len(_PASSIVE_RE.findall(prose))
        self.assertLessEqual(count, PASSIVE_LIMIT, "rewrite new passives in the active voice")


class ProseExtractionTest(unittest.TestCase):
    def test_code_tables_headings_and_inline_code_are_not_prose(self):
        sample = "# Heading words\n\n```\nthis is just code\n```\n| just | table |\n\nUse `just_a_flag` here.\n"
        self.assertEqual([p for _, p in prose_lines(sample)], ["Use CODE here."])

    def test_hyphenated_compounds_are_not_filler(self):
        self.assertIsNone(_phrase_re("just").search("use just-in-time tool discovery"))
        self.assertIsNone(_phrase_re("just").search("see (#just-in-time-tool-discovery)"))
        self.assertIsNotNone(_phrase_re("just").search("it just works"))

    def test_list_items_are_separate_sentences(self):
        sample = "- first item without a stop\n- second item\n"
        self.assertEqual([s for _, s in sentences(sample)], ["first item without a stop", "second item"])

    def test_lowercase_sentence_starts_and_abbreviations(self):
        sample = "Berserk works on its own. berserk-mcp adds tools, e.g. top_cpu.\n"
        self.assertEqual(
            [s for _, s in sentences(sample)],
            ["Berserk works on its own.", "berserk-mcp adds tools, e.g. top_cpu."],
        )

    def test_wrapped_lines_join_into_one_sentence(self):
        sample = "One sentence that wraps\nonto a second line. Next one.\n"
        self.assertEqual(
            [s for _, s in sentences(sample)],
            ["One sentence that wraps onto a second line.", "Next one."],
        )


if __name__ == "__main__":
    unittest.main()

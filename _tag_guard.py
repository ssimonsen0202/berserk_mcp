"""Neutralise forged trust-fence tags in untrusted text.

berserk-mcp wraps telemetry in <untrusted_log_data> and model-authored saved
query descriptions in <generated-description>. Untrusted text must not be
able to close (or open) such a fence, in any spelling a model might read as
the tag: literal, HTML-entity, JSON or URL encoded (at any nesting depth, on any
character, including the tag name), full-width or other NFKC-equivalent
forms, or with invisible characters between letters.

Matching one encoding per character cannot keep up with that list, so this
decodes the whole text to a canonical form first and matches there. Pure: no
I/O, no configuration.
"""

import html
import re
import unicodedata
from urllib.parse import unquote

# Rounds of HTML-entity decoding. Text that still changes after this many
# rounds is treated as hostile: its remaining '&' characters are broken so no
# further decoding by a reader can form a tag.
MAX_DECODE_ROUNDS = 8

# JSON/Python-style escapes (\u003c, \x3c, \/) and URL escapes (%3c) also read
# as '<' to a model, so they are decoded alongside HTML entities.
_BACKSLASH_ESCAPE_RE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|([/<>]))")
_ENCODED_MARK_RE = re.compile(r"[&%\\]")

# Format and filler characters a reader skips over but NFKC keeps.
_INVISIBLE = (
    "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u206a-\u206f\ufeff"
)
_GAP = rf"[\s{_INVISIBLE}]*"


def tag_pattern(name):
    """Compiled regex for an opening or closing `name` tag in canonical text.
    Group 1 is '/' for a closing tag. Attributes are tolerated so that
    <name x="y"> is caught too."""
    letters = _GAP.join(re.escape(c) for c in name)
    # One gap run per position: "<{gap}/?{gap}" would backtrack quadratically
    # over a long run of gap characters.
    return re.compile(rf"<{_GAP}(?:(/){_GAP})?{letters}(?:{_GAP}|\s[^<>]{{0,200}})>", re.IGNORECASE)


def _unescape_backslash(match):
    if match.group(3):
        return match.group(3)
    return chr(int(match.group(1) or match.group(2), 16))


def _decode_once(text):
    text = html.unescape(text)
    text = _BACKSLASH_ESCAPE_RE.sub(_unescape_backslash, text)
    return unicodedata.normalize("NFKC", unquote(text))


def _canonical(text):
    """NFKC + repeated entity/escape decoding. Returns (text, stable)."""
    current = unicodedata.normalize("NFKC", text)
    for _ in range(MAX_DECODE_ROUNDS):
        if not _ENCODED_MARK_RE.search(current):
            return current, True
        decoded = _decode_once(current)
        if decoded == current:
            return current, True
        current = decoded
    return current, False


def neutralize(text, pattern, name):
    """Return `text` with every tag matched by `pattern` replaced by
    '(name)' or '(/name)'.

    Text with no tag in its canonical form is returned NFKC-normalised but
    otherwise unchanged, so ordinary entity-bearing telemetry keeps its
    spelling. Text that does hide a tag is returned in canonical (decoded)
    form with the tags replaced: the encoding was only a disguise.
    """
    normalized = unicodedata.normalize("NFKC", str(text))

    def repl(match):
        return f"({match.group(1) or ''}{name})"

    if not _ENCODED_MARK_RE.search(normalized):
        return pattern.sub(repl, normalized)
    canonical, stable = _canonical(normalized)
    if stable and not pattern.search(canonical):
        return normalized
    if not stable:
        canonical = _ENCODED_MARK_RE.sub(lambda m: f"({m.group(0)})", canonical)
    return pattern.sub(repl, canonical)

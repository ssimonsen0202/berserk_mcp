"""Final execution boundary for user KQL, checked before the bzrk CLI runs.

These checks stay active whatever BERSERK_MCP_KQL_VALIDATION says: that
setting controls advisory/static policy, not this boundary (SECURITY.md,
"Query and process execution"). Kept separate from berserk_mcp so the rule
can be reviewed and scanned on its own. Pure: no I/O, no configuration.
"""

import re

# A query starting with '.' is a Kusto control command.
_CONTROL_RE = re.compile(r"^\s*\.")

# Operators and functions that read a source other than the pipeline input.
# Matched as whole words anywhere outside string literals and comments, not
# only after '|': `where x in (union Secret)` has no pipe before `union`.
_SOURCE_WORD_RE = re.compile(
    r"(?<![\w-])(union|join|lookup|evaluate|find|search|invoke|externaldata|let|macro-expand)(?![\w-])"
    r"|(?<![\w-])(cluster|database|table|toscalar|materialize|view|external_table|materialized_view|"
    r"stored_query_result|entity_group|graph)\s*\(",
    re.IGNORECASE,
)

# String and set operators whose right operand may be a tabular expression
# (Microsoft Learn: in, !in, in~, !in~, has, has_any, has_all, ...). A bare
# identifier there can name another table: `where x in (Secret)`.
_TABULAR_OPERAND_RE = re.compile(
    r"(?<![\w.!~-])(!?(?:in~?|has_any|has_all|(?:has|hasprefix|hassuffix|contains|startswith|endswith)(?:_cs)?))"
    r"(?![\w~-])\s*",
    re.IGNORECASE,
)
_LITERAL_FUNCS = {
    "dynamic",
    "datetime",
    "timespan",
    "time",
    "int",
    "long",
    "real",
    "double",
    "bool",
    "boolean",
    "guid",
    "decimal",
}
_LITERAL_WORDS = {"true", "false", "null"}
_NUMBER_RE = re.compile(r"\d[\w.]*")
_WORD_RE = re.compile(r"[A-Za-z_][\w]*")
_SPACE_RE = re.compile(r"\s*")
_COLON_FUNCS = {"dynamic", "datetime", "time", "timespan"}
_STRING_PLACEHOLDER = "''"


def strip_literals(query):
    """Blank string literals and comments the way the Kusto lexer reads them.

    Returns the query with every string literal replaced by '' and every
    comment by a space, or None when a literal cannot be delimited exactly
    (unterminated). Forms: '..' and ".." with backslash escapes; verbatim
    @'..' / @".." where a backslash is literal and a doubled quote escapes;
    h/H obfuscated prefixes on either; ```..``` multi-line literals.
    """
    out = []
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if query.startswith("//", i):
            end = query.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
            continue
        if query.startswith("```", i):
            end = query.find("```", i + 3)
            if end < 0:
                return None
            i = end + 3
            _emit_string(out)
            continue
        verbatim = ch == "@" and i + 1 < n and query[i + 1] in "'\""
        if verbatim or ch in "'\"":
            if verbatim:
                i += 1
            quote = query[i]
            i += 1
            while True:
                if i >= n:
                    return None
                c = query[i]
                if not verbatim and c == "\\":
                    i += 2
                    continue
                if c == quote:
                    if verbatim and i + 1 < n and query[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            _emit_string(out)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _emit_string(out):
    # Drop an h/H obfuscation prefix so it is not read as an identifier.
    if out and out[-1] in "hH" and (len(out) < 2 or not (out[-2].isalnum() or out[-2] == "_")):
        out.pop()
    out.append(_STRING_PLACEHOLDER)


def _literal_group_error(stripped, start, func=""):
    """Check the parenthesised operand that opens at stripped[start].

    It may contain only literal values: strings, numbers, true/false/null and
    literal functions such as dynamic([...]) or datetime(...). Brackets are
    allowed only inside dynamic(...), so ['Secret'] cannot name a table.
    `func` names the literal function the group belongs to, if any.
    Returns an error string or None.
    """
    stack = []  # one entry per open '(': the literal function before it, or ""
    pending = func  # a literal function name waiting for its '('
    i = start
    n = len(stripped)
    while i < n:
        ch = stripped[i]
        if ch == "(":
            stack.append(pending)
            pending = ""
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        pending = ""
        if ch == ")":
            stack.pop()
            i += 1
            if not stack:
                return None
        elif stripped.startswith(_STRING_PLACEHOLDER, i):
            i += len(_STRING_PLACEHOLDER)
        elif ch in ",+-.":
            i += 1
        elif ch in "[]{}":
            if "dynamic" not in stack:
                return "a bracket outside dynamic(...)"
            i += 1
        elif ch == ":":
            if not _COLON_FUNCS.intersection(stack):
                return "a ':' outside a literal"
            i += 1
        elif ch.isdigit():
            i = _NUMBER_RE.match(stripped, i).end()
        elif _WORD_RE.match(stripped, i):
            word = _WORD_RE.match(stripped, i).group(0)
            i += len(word)
            j = _SPACE_RE.match(stripped, i).end()
            nxt = stripped[j : j + 1]
            lowered = word.lower()
            if lowered in _LITERAL_FUNCS and nxt == "(":
                pending = lowered
            elif not (lowered in _LITERAL_WORDS or ("dynamic" in stack and nxt == ":")):
                return f"the name {word!r}"
        else:
            return f"the character {ch!r}"
    return "an unclosed parenthesis"


def _literal_operand_error(stripped, start):
    """Check an unparenthesised right operand starting at stripped[start]: a
    string, a number, true/false/null, or a literal function call. A bare
    name, a bracket-quoted name or any other function call is refused, since
    it can name or return another table. Returns an error string or None."""
    if stripped.startswith(_STRING_PLACEHOLDER, start) or stripped[start : start + 1].isdigit():
        return None
    if stripped[start : start + 1] == "-" and stripped[start + 1 : start + 2].isdigit():
        return None
    word = _WORD_RE.match(stripped, start)
    if not word:
        return f"{stripped[start : start + 1]!r}" if start < len(stripped) else "a missing value"
    lowered = word.group(0).lower()
    if lowered in _LITERAL_WORDS:
        return None
    j = _SPACE_RE.match(stripped, word.end()).end()
    if lowered in _LITERAL_FUNCS and stripped[j : j + 1] == "(":
        return _literal_group_error(stripped, j, lowered)
    return f"the name {word.group(0)!r}"


def source_violation(query):
    """Return a description of the first construct that can read a source
    other than the pipeline input, or None. `query` is raw user KQL."""
    stripped = strip_literals(query)
    if stripped is None:
        return "an unterminated string literal"
    m = _SOURCE_WORD_RE.search(stripped)
    if m:
        return f"operator {(m.group(1) or m.group(2)).lower()!r}"
    for m in _TABULAR_OPERAND_RE.finditer(stripped):
        operator = m.group(1).lower()
        rest = stripped[m.end() :]
        if rest.startswith("("):
            problem = _literal_group_error(stripped, m.end())
            if problem:
                return f"{problem} in the operand of {operator!r}"
        else:
            problem = _literal_operand_error(stripped, m.end())
            if problem:
                return f"{problem} as the operand of {operator!r}"
    return None


def check(query, table):
    """Return an error message, or None when `query` may be passed to bzrk.

    - No semicolon anywhere, including inside a string literal: a second
      statement must never reach the CLI.
    - No control commands.
    - The query must start with the configured table. It is passed to bzrk as
      a positional argv element, and one starting with '-' could be parsed as
      an option instead (e.g. a stray "--profile x"), changing what runs.
    - Nothing after the prefix may read another source (source_violation).
    """
    if ";" in query:
        return "invalid KQL: semicolons are not allowed in user queries"
    if _CONTROL_RE.match(query):
        return "invalid KQL: control commands are not allowed in user queries"
    if not re.match(r"^\s*" + re.escape(table) + r"\b", query):
        return f"invalid KQL: query must start with '{table} | ...' (got: {query[:40]!r})"
    violation = source_violation(query)
    if violation:
        return f"invalid KQL: {violation} is not allowed in user queries (only the configured table may be read)"
    return None

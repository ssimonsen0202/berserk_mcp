"""Final execution boundary for user KQL, checked before the bzrk CLI runs.

These checks stay active whatever BERSERK_MCP_KQL_VALIDATION says: that
setting controls advisory/static policy, not this boundary (SECURITY.md,
"Query and process execution"). Kept separate from berserk_mcp so the rule
can be reviewed and scanned on its own. Pure: no I/O, no configuration.
"""

import re

# A query starting with '.' is a Kusto control command.
_CONTROL_RE = re.compile(r"^\s*\.")


def check(query, table):
    """Return an error message, or None when `query` may be passed to bzrk.

    - No semicolon anywhere, including inside a string literal: a second
      statement must never reach the CLI.
    - No control commands.
    - The query must start with the configured table. It is passed to bzrk as
      a positional argv element, and one starting with '-' could be parsed as
      an option instead (e.g. a stray "--profile x"), changing what runs.
    """
    if ";" in query:
        return "invalid KQL: semicolons are not allowed in user queries"
    if _CONTROL_RE.match(query):
        return "invalid KQL: control commands are not allowed in user queries"
    if not re.match(r"^\s*" + re.escape(table) + r"\b", query):
        return f"invalid KQL: query must start with '{table} | ...' (got: {query[:40]!r})"
    return None

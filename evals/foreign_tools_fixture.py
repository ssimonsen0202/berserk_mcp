"""A small, realistic fixture representing tools from two unrelated MCP
servers (issue #78) -- used to test whether berserk-mcp's own tools still
route correctly when a real agent has other, unrelated servers loaded in
the same context, which is how most real deployments actually look. Never
executed; only present as schema noise for the model to route around.

Shaped like OpenAI-style function tools (name/description/parameters),
matching to_openai_tools()'s output shape in run_eval.py, converted to
Anthropic shape the same way to_anthropic_tools() converts berserk-mcp's
own tools.
"""

FOREIGN_TOOLS = [
    # -- a plausible Slack MCP server --
    {
        "name": "slack_list_channels",
        "description": "List Slack channels the bot has access to, with member counts and topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_archived": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "slack_send_message",
        "description": "Post a message to a Slack channel or thread. Use for 'notify the team' or 'post an update'.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID or name"},
                "text": {"type": "string"},
                "thread_ts": {"type": "string", "description": "optional: reply in this thread"},
            },
            "required": ["channel", "text"],
        },
    },
    {
        "name": "slack_search_messages",
        "description": "Full-text search across Slack message history. Use for 'find that message about X' or 'what did the team say about Y'.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "channel": {"type": "string", "description": "optional: limit to one channel"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "slack_get_user_status",
        "description": "Look up a Slack user's current status and presence (active/away).",
        "parameters": {
            "type": "object",
            "properties": {"user": {"type": "string"}},
            "required": ["user"],
        },
    },
    # -- a plausible GitHub MCP server --
    {
        "name": "github_list_issues",
        "description": "List open or closed issues for a repository, optionally filtered by label or assignee.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                "label": {"type": "string"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "github_create_pr",
        "description": "Open a new pull request against a repository branch. Use for 'open a PR' or 'submit these changes for review'.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "base": {"type": "string"},
                "head": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["repo", "base", "head", "title"],
        },
    },
    {
        "name": "github_search_code",
        "description": "Search source code across a repository or organization for a pattern.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "repo": {"type": "string", "description": "optional: limit to one repo"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "github_get_file",
        "description": "Fetch the raw contents of one file at a given ref (branch, tag, or commit SHA).",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "ref": {"type": "string", "default": "main"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "github_list_prs",
        "description": "List pull requests for a repository, optionally filtered by state or author.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "merged", "all"], "default": "open"},
            },
            "required": ["repo"],
        },
    },
]


def to_openai_foreign_tools():
    return [{"type": "function", "function": t} for t in FOREIGN_TOOLS]


def to_anthropic_foreign_tools():
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
        for t in FOREIGN_TOOLS
    ]

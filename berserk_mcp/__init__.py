"""berserk-mcp — a Model Context Protocol server for Berserk observability.

Lets an LLM answer observability questions by *calling tools* instead of
hand-authoring KQL. Each tool wraps a verified Kusto/KQL query, so the model
cannot mangle field names or table references — the determinism is the point.

Transport: newline-delimited JSON-RPC 2.0 over stdio (the MCP stdio transport).
Dependencies: none. Pure Python standard library, so it runs anywhere `bzrk`
(the Berserk CLI) is installed, including Windows.

It shells out to the `bzrk` CLI for every query. The Berserk bearer token lives
only in `bzrk`'s own config (typically 0600) and is never read, stored, or
logged by this server.

Configuration (all optional, via environment):
  BZRK_BIN                 trusted path/name of the bzrk binary   (default: "bzrk")
  BZRK_PROFILE             bzrk profile to query                  (default: "local")
  BZRK_TIMEOUT             per-query timeout in seconds           (default: "120")
  BERSERK_WORKER_JITTER_SECONDS  max random startup delay for --worker (default: "7200")
  BERSERK_MCP_TOOL_BUDGET_SECONDS interactive tools/call budget (default: "10")
  BERSERK_MCP_FAIL_COOLDOWN_SECONDS identical timeout suppression (default: "30")
  BERSERK_MCP_CACHE_TTL_SECONDS read-only result cache TTL (default: "120")
  BERSERK_MCP_CACHE_MAX_ENTRIES entries kept in the result cache and the fail-cooldown table (default: "256")
  BERSERK_MCP_KQL_VALIDATION validation policy: off/warn/strict (default: "warn")
  BERSERK_MCP_KQL_LIVE_VALIDATION enable validate_kql mode=live (default: "0")
  BERSERK_MCP_MAX_CONCURRENT_QUERIES in-process query concurrency (default: "2")
  BERSERK_MCP_KQL_MAX_CHARS maximum user KQL length (default: "50000")
  BERSERK_MCP_KQL_MAX_ROWS recommended arbitrary-query row bound (default: "2000")
  BERSERK_MCP_KQL_STATS stats handling: off/auto/required (default: "auto")
  BERSERK_MCP_MAX_RESULT_BYTES hard cap for bzrk stdout (default: 10485760)
  BERSERK_MCP_MAX_OUTPUT_CHARS characters of search/saved-query result sent to the model; 0 = no cap (default: "40000")
  BERSERK_MCP_FINOPS_REDACT_ENTROPY enable entropy redaction in FinOps free text (default: "0")
  BERSERK_TABLE            the Berserk table to query             (default: "default")
  BERSERK_MCP_LEARNED_PATH where saved queries persist  (default: per-user config dir)

Parser factory (LLM-driven parser generation, see parser_factory.py) adds
outbound HTTP to LLM providers -- all optional, a provider with no key
configured is skipped:
  BERSERK_LLM_LADDER          provider order for generation    (default: "hermes,openai,anthropic")
  HERMES_API_KEY               bearer token for the Hermes endpoint
  BERSERK_LLM_HERMES_URL       Hermes chat-completions endpoint (else local
                               llm_config.json, else http://localhost:3000/...;
                               set via: berserk-mcp --set-hermes-url <URL>)
  BERSERK_LLM_HERMES_MODEL     Hermes model id            (default: auto-discovered via /api/models)
  OPENAI_API_KEY                OpenAI API key
  BERSERK_LLM_OPENAI_MODEL     OpenAI model                     (default: "gpt-4o")
  ANTHROPIC_API_KEY             Anthropic API key
  BERSERK_LLM_ANTHROPIC_MODEL  Anthropic model                  (default: "claude-opus-4-8")
  BERSERK_LLM_TIMEOUT          per-LLM-call timeout in seconds  (default: "120")

This is an unofficial, community-maintained integration. It is not affiliated
with or endorsed by the Berserk project.

Package layout (v1.37.0): the code lives in the modules of this package,
lowest layer first in berserk_mcp/_facade.py (LAYERS). `berserk_mcp.NAME`
still works for every name: the facade reads and writes NAME in the module
that owns it, so tests and scripts that patch `berserk_mcp.NAME` reach
every reader.
"""

import sys

from berserk_mcp import _facade
from berserk_mcp._version import __version__ as __version__

_facade.install(sys.modules[__name__])

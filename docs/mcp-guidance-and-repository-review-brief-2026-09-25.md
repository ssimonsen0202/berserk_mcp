# Berserk MCP Guidance and Repository Review Brief

**Date:** 2026-09-25

**Review goal:** Learn from mature MCP, Agent Skills, security, and registry repositories, then review `berserk-mcp` for a lean, efficient, secure, agent-native design.

**Review mode:** Analysis and review only. Do not modify code unless the user explicitly approves a follow-up implementation plan.

## Claude Code handoff

Use this prompt in Claude Code from the repository root:

```text
Read and follow @docs/mcp-guidance-and-repository-review-brief-2026-09-25.md.

Review the current berserk-mcp repository against the guidance and references in that file. Do not modify code.

First read AGENTS.md, README.md, docs/claude-code-review-feedback-loop.md, docs/tool-tiers-implementation-spec.md, docs/result-envelope-implementation-spec.md, docs/agent-behavior-eval.md, and docs/mcp-live-readonly-eval.md.

Produce a structured review with:

1. Executive summary
2. What Berserk already does better than typical MCP servers
3. P1/P2 findings with file:line, impact, evidence, and repro command
4. Lean-tooling and context-cost findings
5. MCP protocol and compatibility findings
6. Security, PII, prompt-injection, and supply-chain findings
7. Tool-contract and correlation findings
8. Test and evaluation gaps
9. Recommended implementation sequence
10. Explicit items that are not problems or should not be changed

Use the review loop in docs/claude-code-review-feedback-loop.md. Verify every suspected finding against the code and real output before reporting it. Do not treat repository stars as proof of quality. Do not recommend a generic arbitrary-KQL, shell, SSH, or execute-anything tool.
```

## Review principles

The objective is not to make Berserk expose more capabilities. The objective is to make the existing capabilities easier for agents to select correctly, cheaper to expose, safer to operate, and easier to evaluate.

The review must preserve these existing strengths:

- Fixed verified queries instead of generic text-to-KQL
- Standard-library-only implementation
- Role-aware tool visibility
- Small/deep tier separation
- Query budgets, timeouts, cache, and concurrency guards
- Structured result-envelope direction
- KQL validation and schema grounding
- Trace and SRE/SOC tools
- Secret redaction and public-artifact leak tests
- Offline protocol smoke and agent-behavior evaluations
- No shell escape, arbitrary SSH, or arbitrary command execution

## External references to inspect

Review these as design references, not as requirements to copy wholesale.

### MCP protocol and SDKs

- [MCP specification and documentation](https://github.com/modelcontextprotocol/modelcontextprotocol) - protocol contracts, lifecycle, security model, and compatibility.
- [Official MCP servers](https://github.com/modelcontextprotocol/servers) - reference server patterns.
- [Official Python SDK](https://github.com/modelcontextprotocol/python-sdk) - Python protocol implementation patterns. Berserk should preserve its zero-dependency policy unless a separate decision approves otherwise.
- [Official TypeScript SDK](https://github.com/modelcontextprotocol/typescript-sdk) - useful comparison for schemas and transport behavior.
- [MCP registry](https://github.com/modelcontextprotocol/registry) - discoverability, metadata, and publication patterns.
- [Microsoft MCP for Beginners](https://github.com/microsoft/mcp-for-beginners) - cross-language examples and basic secure workflow patterns.

### Skills and agent workflow design

- [Anthropic Skills](https://github.com/anthropics/skills) - official skill packaging, `skill-creator`, and `mcp-builder` patterns.
- [OpenAI Skills](https://github.com/openai/skills) - Codex skill catalog and portable skill structure.
- [Agent Skills specification](https://github.com/agentskills/agentskills) - portability and frontmatter conventions.
- [obra/superpowers](https://github.com/obra/superpowers) - planning, TDD, implementation, review, and handoff workflow patterns.
- [Addy Osmani agent-skills](https://github.com/addyosmani/agent-skills) - production engineering skills, code review, and quality gates.
- [GitHub awesome-copilot](https://github.com/github/awesome-copilot) - custom agents, skills, instructions, and MCP configurations.
- [Vercel skills tool](https://github.com/vercel-labs/skills) - skill discovery and installation mechanics.
- [Composio Codex skills](https://github.com/ComposioHQ/awesome-codex-skills) - Codex-oriented workflow catalog.

### Security and governance

- [MCP security model](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/SECURITY.md) - trust model, input validation, access control, and tool invocation responsibilities.
- [SlowMist MCP Security Checklist](https://github.com/slowmist/MCP-Security-Checklist) - practical security checklist.
- [Google MCP security](https://github.com/google/mcp-security) - security-oriented reference material.
- [Pipelock](https://github.com/luckyPipewrench/pipelock) - agent/MCP egress, SSRF, exfiltration, and prompt-injection firewall concepts.
- [Trail of Bits MCP context protector](https://github.com/trailofbits/mcp-context-protector) - context and tool-boundary protection ideas.
- [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) - scanning Agent Skills for prompt injection, data exfiltration, and supply-chain risks.
- [Cloudflare security-audit-skill](https://github.com/cloudflare/security-audit-skill) - machine-readable, multi-phase security review patterns.
- [Archestra](https://github.com/archestra-ai/archestra) - enterprise MCP registry, gateway, and guardrail concepts.
- [Docker MCP registry](https://github.com/docker/mcp-registry) - registry and packaging patterns.

## Current Berserk design to evaluate

### Fixed-query model

Verify that fixed intent tools remain the default path for common questions. Assess whether any new or existing escape hatch makes models author arbitrary KQL unnecessarily.

Review:

- `SIMPLE`
- saved-query projection
- `search`
- `validate_kql`
- schema discovery
- parser factory
- discovery worker
- CanonLoom bridge

### Role and tier visibility

Verify that:

- The role predicate is authoritative and reused by both discovery and direct calls.
- Small-tier agents see fixed, verified, bounded tools.
- Deep-tier authoring and lifecycle tools do not leak into operational lanes.
- Hidden tools return non-leaking errors.
- Startup and doctor output makes the active role/tier obvious.
- Tool-list size and serialized bytes are measured by lane and tier.

### Tool contracts

For each high-traffic tool, inspect whether the schema and description communicate:

- Purpose
- Intended use
- Non-use cases
- Required scope
- Time-window semantics
- Cost or query-budget implications
- Empty-result meaning
- Partial/failure behavior
- Suggested follow-up tools
- Data sensitivity
- Read-only or mutating behavior

Do not add prose globally when the behavior belongs in the tool contract.

### Result envelopes and evidence

Check whether outputs preserve:

- Resolved time window
- Row count or explicit unknown state
- Source and query kind
- Freshness
- Empty versus failed versus partial state
- Stable evidence reference
- Redaction status
- Suggested next steps

Legacy text compatibility must remain intact unless an explicit migration is approved.

### Correlation

Review whether Berserk can deterministically correlate:

- Metrics and logs
- Traces and span trees
- Errors and deployments
- Host, service, container, and trace identities
- Before/during/after windows
- Contradictory signals
- Missing telemetry

The model should consume normalized evidence, not manually join large raw result sets whenever Berserk can do the join safely and reproducibly.

## Questions Claude Code must answer

### Lean and efficient

- Which tools can be merged without making intent ambiguous?
- Which tool descriptions are too verbose or redundant?
- Which tools should move from always-visible to discoverable/deep-only?
- How many tool definitions and serialized bytes does each role/tier expose?
- Are default time windows safe and visible?
- Are result sizes, query budgets, and concurrency bounded everywhere?
- Are caches keyed by all semantically relevant inputs?
- Are there duplicate queries or duplicate parsers that can be removed?
- Does any tool perform work the caller cannot control through bounded inputs?

### MCP correctness

- Are `initialize`, `notifications/initialized`, `ping`, `tools/list`, and `tools/call` behaviorally consistent across protocol modes?
- Are modern and legacy envelopes both tested?
- Are tool annotations accurate and non-authoritative?
- Are errors structured, non-leaking, and distinguishable from empty results?
- Are hidden tools protected at direct-call time as well as discovery time?

### Security and privacy

- Can telemetry content inject instructions into the agent?
- Can a tool return secrets, tokens, raw prompts, or private deployment markers?
- Can arbitrary KQL, shell, SSH, URLs, or filesystem paths enter an operational lane?
- Can saved queries or generated parsers create a confused deputy?
- Are generated tool descriptions or query packs reviewed before becoming visible?
- Are data sensitivity and redaction status explicit in result envelopes?
- Are raw-content paths separated from aggregate operational paths?
- Does the server fail closed when validation, schema, or authorization evidence is missing?

### Evaluation quality

- Does each new contract have unit tests?
- Does each role/tier change have discovery and direct-call tests?
- Are empty, partial, unavailable, timeout, overflow, and malformed-response cases covered?
- Are fixture tests platform-independent?
- Are real command output shapes verified rather than guessed?
- Do agent behavior evals measure tool choice, argument correctness, safety, efficiency, evidence quality, and uncertainty calibration?
- Are live smoke tests explicitly read-only?

## Findings format

Report each finding as:

```markdown
### P1/P2: <short title>

- File:line: <location>
- Category: protocol | safety | privacy | tool-contract | efficiency | correlation | testing
- Impact: <what can go wrong>
- Evidence: <code path, test, or observed output>
- Repro: <focused command or test>
- Why existing tests miss it: <explanation>
- Recommended fix: <bounded recommendation>
- Compatibility risk: low | medium | high
```

Also include:

- Findings verified false-positive
- Strengths preserved
- Unexercised areas
- Suggested follow-up plan

Use the repository review loop in `docs/claude-code-review-feedback-loop.md`: verify suspected findings against code and real output before recommending changes.

## Review boundaries

Do not:

- Implement fixes during this review
- Add dependencies
- Change query strings
- Change permissions or production configuration
- Connect to a live Berserk profile unless the user explicitly authorizes a read-only smoke test
- Publish private incident or topology information
- Treat GitHub star counts as evidence of security or correctness
- Copy external repository code without checking license, provenance, and compatibility

## Desired final outcome

The review should answer whether `berserk-mcp` is:

1. Lean enough for small/local models.
2. Typed and self-guiding enough for reliable tool selection.
3. Safe against arbitrary query, shell, prompt-injection, and data-exfiltration paths.
4. Correctly separated into operational and deep authoring capabilities.
5. Able to produce reproducible evidence for incident correlation and RCA.
6. Compatible with Claude Code, Codex, and ordinary MCP clients.

The review should end with a short, prioritized implementation plan. No code should be changed until that plan is explicitly approved.

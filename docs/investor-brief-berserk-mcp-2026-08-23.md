# Berserk MCP: the agent-facing layer of Berserk, and what it means for go-to-market

**Status: DRAFT** — investor briefing prepared 2026-08-23. Not yet reviewed/approved for external distribution.

How the MCP server complements the core Berserk platform, where that positions Berserk against the current observability and AI-governance market, and what it implies for go-to-market from here.

---

## 1. Product: Berserk MCP isn't a feature bolted onto Berserk — it's the loop that closes it

Berserk is a unified observability platform: infrastructure and security telemetry ingested via OTLP, queried through a KQL-style engine, with mature SRE and SOC analysis built on top. Berserk MCP exposes that same analytical depth to AI agents as directly callable tools — and, just as importantly, turns Berserk's lens back onto the agents themselves.

```mermaid
flowchart LR
    Infra["Infrastructure\nlogs · metrics · traces"] -->|OTLP| Core
    Agents["AI Agents\nClaude Code · Codex · ..."] -->|OTLP| Core
    Core["Berserk Core\ningestion · KQL engine\nSRE · SOC · FinOps"] --> MCP["berserk-mcp\n69 tools, tiered for\nreliability & data-fencing"]
    MCP -.->|agents query back for live answers| Agents
```

*Fig. 1 — Infrastructure and AI-agent telemetry share one ingestion path; agents query the same platform back out.*

This reflexive design is the point. berserk-mcp doesn't just let an agent ask "what's the error rate on checkout-service" and get a real answer instead of a hallucination. As of this week it also ingests the agents' own session activity — Claude Code today, Codex CLI newly added, more planned[^1] — back into Berserk. The same platform that tells an SRE what their infrastructure is doing now tells an engineering leader what their AI coding agents are doing, and what it's costing.

### Shipped today
- SRE & SOC analysis, mature — the pre-existing core
- Untrusted-data fencing: log/trace content is treated as attacker-influenceable, not trusted, by design
- Tool-tiering validated empirically this week across 8 real models — small/cheap models included, not assumed
- Multi-agent telemetry ingestion (Claude Code + Codex, live-verified against real session data)

### The reflexive layer
- Per-feature and per-project AI cost economics, today
- Evidence-backed harness recommendations with an approve/reject audit trail
- Live quota-window visibility (planned, issue #43)
- Active kill-switch for runaway agent behavior (planned, issue #44)

---

## 2. Market: the market splits into two camps. Berserk is building the intersection.

The LLM observability market is sized at roughly $2.69B in 2026, heading toward $9.26B by 2030 — a 36.2% CAGR.[^2] But sizing the category obscures how it's actually structured today, and that structure is the opportunity.

> "Most production teams pick a primary [LLM-trace] observability platform … and pair it with the broader infrastructure observability layer … for whole-stack coverage."[^2] That pairing is two purchases, two consoles, two data models — for one workflow.

**Camp one — AI-native trace tools.** Langfuse, LangSmith, Arize/Phoenix, Braintrust: deep on the LLM call itself — spans, evals, node-by-node execution graphs. Genuinely strong at what they do. Shallow, by design, on infrastructure — they trace the agent, not the fleet it's running on.

**Camp two — infra incumbents, now with MCP.** This is where the ground moved under the "MCP is greenfield" assumption: Datadog shipped a GA MCP server in March 2026; Grafana shipped one covering Prometheus, Loki, and Tempo in early 2026.[^3] Deep infra, and now agents can query it. But the MCP layer, in both cases, reads as a pass-through onto existing dashboard queries — letting an agent ask about your infrastructure, not governing what the agent itself is doing or costing.

```mermaid
quadrantChart
    title Infra depth vs. AI-agent governance depth
    x-axis Low infra depth --> High infra depth
    y-axis Low agent governance --> High agent governance
    quadrant-1 The intersection
    quadrant-2 Trace tools
    quadrant-3 Niche point tools
    quadrant-4 Infra + MCP pass-through
    LangSmith: [0.22, 0.55]
    Langfuse: [0.2, 0.5]
    Arize/Phoenix: [0.25, 0.6]
    Datadog MCP: [0.8, 0.3]
    Grafana MCP: [0.82, 0.25]
    Berserk: [0.85, 0.85]
```

*Fig. 2 — Positioning by infra depth vs. AI-agent governance depth. Approximate, based on publicly described capabilities.*

AI-agent cost and behavior governance is not a niche concern to build the intersection around — it's becoming a mainstream enterprise priority in its own right. AWS launched a dedicated FinOps agent for AI cost governance in June 2026;[^4] industry survey data puts AI cost-management practice adoption near-universal (98%) among organizations running AI workloads.[^4] That validates demand for the governance layer Berserk has a head start on — and signals real, growing competitive attention at that layer specifically, not a permanently open field.

---

## 3. Go-to-market: what this changes about how Berserk should be sold

The shipped platform plus the near-term roadmap supports a specific reframe: stop selling "an observability platform with an AI feature," and start selling "the governance layer for the AI coding agents your engineering org has already adopted."

**Why the reframe matters.** Sold as observability, Berserk competes head-on with Datadog's incumbent budget line. Sold as AI-agent governance, it's a new, urgent, under-served line item — and the "we already have Datadog" objection stops being a blocker, because agent-governance is complementary even to shops keeping their existing infra tool.

### Two go-to-market motions, not one

| | Expand existing accounts | New-logo wedge |
|---|---|---|
| **Who** | Current SRE/SOC customers | Platform-eng / security buyers |
| **Motion** | Agent-governance as a natural extension of a platform they already run — no new vendor relationship | A motivated, reachable persona who've rolled out Claude Code / Copilot / Codex with zero cost or behavior visibility |
| **Trigger** | Eng org adopts more coding agents on data already flowing through the same pipeline | Independent of whether they use Berserk for infra today |

### The roadmap reads as a trajectory, not a feature list

| Today — Observe & analyze | Near-term — Quantify & alert | Mid-term — Govern & defend |
|---|---|---|
| Multi-agent telemetry ingestion | Live quota-window visibility (#43) | Active kill-switch for runaway agents (#44) |
| Cost / spend economics per feature & project | Weekly digest with one actionable tip (#46) | Hash-chained, exportable audit ledger (#17 / #19) |
| Reliability-engineered tool exposure | | ATT&CK-mapped agent security detection, portable to Sentinel/Defender (#29 / #30) |

That progression — from watching, to quantifying, to actively governing and defending — is a credible platform story, not a scramble of disconnected features. It's also the specific sequence a security-conscious buyer in the new-logo wedge would ask for, in the order they'd ask for it.

---

### Sources

[^1]: Codex CLI ingestion adapter shipped and live-verified against real session data this week (issue #42); OpenCode planned next.
[^2]: [MarkTechPost, "Top LLM Observability and Evaluation Platforms in 2026" (Aug 2026)](https://www.marktechpost.com/2026/08/09/top-llm-observability-and-evaluation-platforms-in-2026-langfuse-langsmith-braintrust-arize-and-more-compared/); market-dynamics framing per the same survey of current platforms.
[^3]: [Datadog MCP Server documentation](https://docs.datadoghq.com/mcp_server/); [Grafana Labs, MCP server for Prometheus/Loki/Tempo](https://grafana.com/blog/ai-observability-MCP-servers/).
[^4]: [SiliconANGLE, "AWS launches FinOps agent to bring AI cost governance to cloud spend" (Jun 2026)](https://siliconangle.com/2026/06/11/aws-launches-finops-agent-bring-ai-cost-governance-cloud-spend-finopsx/); adoption figure per FinOps Foundation "State of FinOps 2026" survey commentary.

### Related

- Full designed version (diagrams, print-page layout): published as a Claude Artifact, link held by whoever prepared this draft — not embedded here since artifact URLs are private/session-scoped.
- [`model-routing-cost-validation-2026-08-23.md`](model-routing-cost-validation-2026-08-23.md) — the underlying eval data behind the "tool-tiering validated empirically... across 8 real models" claim in section 1.

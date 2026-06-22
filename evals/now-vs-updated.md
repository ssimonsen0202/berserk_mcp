# MCP behavior: deployed-now vs. updated

Honest evaluation of what the pending spec-polish deploy actually changes. Written to
set expectations *before* deploying — so a "no visible difference" result isn't
misread as "the change was pointless."

## What's different

| Change | Changes the model's *answers*? | What it actually does |
|---|---|---|
| **`instructions`** (server-level string at init) | **Yes — the only one that does** | Injected into the model's context: "call tools, don't write KQL; prefer a specific tool over `search`; host metrics ≠ container metrics; use `save_query` for recurring questions." Primes routing + the learning loop. |
| **`annotations`** (`readOnlyHint`, etc.) | No — affects the **client** | Lets Hermes auto-approve read-only tools without a gate. Operational safety, not answer quality. |
| **`title`** fields | Negligible | Display label in some clients; at most a tiny disambiguation nudge. |
| **protocol `2024-11-05` → `2025-06-18`** | No | Compatibility/correctness. Hermes negotiates either way. |

Tools, KQL, and the 22-tool surface are **identical**. So the entire behavioral
question reduces to: **does `instructions` change what the model does?**

## Does `instructions` move the needle?

The live eval already put the cheap model (`gpt-4.1-mini`) at ~100% on its 4 question
classes **without** `instructions`. On those, there is no room to improve — it's already
at ceiling. So measuring now-vs-updated on the *easy* questions will show **no
difference**, and that's expected, not a failure.

Where `instructions` plausibly helps is the **long tail the basic eval doesn't touch**:

1. **Anti-`search` bias** — an oddly-phrased question that a dedicated tool covers.
   Without the nudge, the model occasionally reaches for raw `search` (and may mangle
   KQL); the nudge pushes it to the fixed tool.
2. **Loop-triggering** — "I don't think there's a tool for X, work it out." The nudge
   explicitly points at `discover_schema` → `search` → `save_query`. Without it, the
   model is less likely to self-extend.
3. **Host vs. container under trickier phrasing** — the nudge states the distinction
   outright, so ambiguous wording ("what's hammering the box?") routes more reliably.

Net prediction: **small, real gains on routing edge-cases and the self-extending loop;
zero change on everyday questions.** The annotations give an independent operational win
(safe auto-approve) regardless of answer quality.

## How to actually measure it (A/B — cheap, ~15 min)

Run the **same** cheap model against the **same** questions, once with the deployed MCP
(no `instructions`) and once with the updated MCP (`instructions` on). Don't reuse the
easy 4 — they're at ceiling. Use edge-case questions designed to expose the difference:

- **Anti-search:** "Give me the error breakdown across services for the last day."
  (Should hit `errors_by_service`, not raw `search`.)
- **Ambiguous host/container:** "What's hammering the box right now?"
  (Should pick `host_cpu` or ask, not silently list containers.)
- **Loop-trigger:** "Is anything tracking S3 fetch latency? If there's no tool, figure
  out the metric and show me." (Should reach for `list_metrics`/`discover_schema`.)
- **Save-query:** after the loop answers, "save that so I can re-run it as `s3_latency`."
  (Should call `save_query`.)

Score over **N≥3 runs each**: tool-selection accuracy, raw-`search` rate (lower = better),
loop-trigger rate, save_query trigger. The delta between the two MCP versions *is* the
value of `instructions`.

Run command shape (on VM-A): `sudo -i -u assistant hermes -z "<question>"` against the
deployed version, then deploy the update and repeat. (The deployed file has no
`instructions`; the updated one does — that's the only variable.)

## Bottom line / deploy call

- **Low risk, reversible, worth deploying** — the annotations alone (safe auto-approve)
  and the protocol bump justify it; `instructions` primes the discovery loop we just
  built.
- **But don't expect a visible jump on everyday questions** — the cheap model is already
  at ceiling there. If you A/B only the easy questions, you'll see "no difference" and
  wrongly conclude it's pointless. Measure on the edge-cases above.
- If the edge-case A/B shows the no-`instructions` version reaching for raw `search` or
  missing the loop more often, that's the concrete payoff — and the reason to keep
  `instructions` in the deployed build.

# Fleet tuning result — 2026-07-23

The measured defaults are documented in
[`docs/fleet-controls-eval-2026-07-23.md`](../docs/fleet-controls-eval-2026-07-23.md).

| Parameter | Recommendation | Rule/result |
|---|---:|---|
| `BERSERK_WORKER_JITTER_SECONDS` | 7200 s | Smallest tested jitter with P(peak > 3 in 10 s) < 5% at 100 workers: 4.0%. |
| `BERSERK_MCP_TOOL_BUDGET_SECONDS` | 10 s | Five-repeat homelab sweep overall p95 1.75 s; `ceil(1.5 × p95)` with 10 s floor. |
| `BERSERK_MCP_CACHE_TTL_SECONDS` | 120 s | Smallest tested TTL at ≥80% of maximum synthetic hit rate: 69.0% hit rate, p95 staleness 112.6 s. |
| `BERSERK_MCP_FAIL_COOLDOWN_SECONDS` | 30 s | Smallest tested cooldown absorbing ≥90% identical-retry storm calls, with zero changed-argument delay. |

All four values are now the code defaults and are overrideable with environment
variables. The live latency output is in `evals/results/latency_eval.json`.

# Fleet controls and Phase 3 evaluation — 2026-07-23

## Phase 2 bounded controls

The offline simulations are deterministic (1,000+ trials) and write their
machine-readable results under `evals/results/`:

| Control | Selected value | Evidence |
|---|---:|---|
| Worker startup jitter | 7,200 s | `jitter_sim.json`; at 100 workers this is the smallest tested value with P(peak > 3 in 10 seconds) below 5% (4.0%). |
| Interactive tool budget | 10 s | Five repeats across every read-only fixed-query tool: overall p95 1.75 s; `ceil(1.5 × p95)` floors to 10 s, with no tool over budget. |
| Read-only result cache | 120 s | 69.0% replay hit rate with p95 staleness 112.6 s; 300 s reaches 82.0% but increases staleness. |
| Identical-timeout cooldown | 30 s | Absorbs five repeated storm calls without delaying changed-argument retries. |

The first broad-window sweep was stopped after `claude_cost_report` exceeded a
practical interactive runtime. The evaluator now defaults to a bounded
15-minute window. A five-repeat live sweep completed successfully with zero
errors: overall p95 was 1.75 seconds, so the rule
`ceil(1.5 × overall_p95), floor 10s, cap BZRK_TIMEOUT` recommends a 10-second
interactive budget. The implementation keeps the safer configured 60-second
ceiling until an operator explicitly adopts the tighter value; raw results are
in `evals/results/latency_eval.json`.

## Phase 3 live probes

Against `BZRK_PROFILE=homelab`:

- `detect_anomalies` parsed and returned zero-filled event, anomaly, score, and
  baseline arrays from `make-series` + `series_decompose_anomalies`.
- `forecast_capacity` parsed native fit arrays. It refused a weak/downward
  OpenClaw trend (`R²=0.156`, negative slope) and reported a reliable HermesRuntime
  upward trend (`R²=0.688`) without inventing a ceiling date.
- `find_similar` degraded safely because this cluster's parser rejects
  `similarto`; the response directs users to exact-term `search` instead.

The router harness plumbing and mock baseline run, but a real model gate was not
run: neither `OPENAI_API_KEY` nor a local Ollama endpoint is configured in this
environment. The mock baseline is retained in
`evals/results/mock_mock-20260723-113523.json` for repeatability, not as a
quality claim for a production model.

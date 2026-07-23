#!/usr/bin/env python3
"""Live latency eval for read-only fixed-query tools."""
import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def percentile(values, p):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))]


def run(repeats=5, since="15m ago"):
    import berserk_mcp as bm
    bm.CACHE_TTL_SECONDS = 0
    bm.FAIL_COOLDOWN_SECONDS = 0
    bm.TOOL_BUDGET_SECONDS = 0
    fixed = sorted(set(bm.SIMPLE) | {
        "sre_service_health", "soc_timeline", "claude_loop_check",
        "claude_model_fit", "claude_token_burn", "claude_cost_report",
        "claude_session_deep_dive", "claude_workflow_insights",
    })
    args = {
        "sre_service_health": {"service": "claude-code"},
        "soc_timeline": {"service": "claude-code"},
        "claude_session_deep_dive": {"session_id": "none"},
    }
    rows = []
    for name in fixed:
        samples = []
        errors = 0
        call_args = dict(args.get(name, {}))
        if since:
            call_args["since"] = since
        for _ in range(max(1, repeats)):
            t0 = time.perf_counter()
            _, is_err = bm.handle_call(name, dict(call_args))
            samples.append(time.perf_counter() - t0)
            errors += int(is_err)
        rows.append({"tool": name, "p50": percentile(samples, .5),
                     "p95": percentile(samples, .95), "max": max(samples),
                     "errors": errors})
    all_samples = [v for row in rows for v in [row["p50"], row["p95"], row["max"]]]
    overall_p95 = percentile(all_samples, .95)
    timeout = float(bm.DEFAULT_TIMEOUT)
    recommendation = min(timeout, max(10.0, math.ceil(1.5 * overall_p95)))
    offenders = [r["tool"] for r in rows if r["p95"] > recommendation]
    return {"rows": rows, "overall_p95": overall_p95,
            "recommendation_seconds": recommendation, "offenders": offenders,
            "rule": "ceil(1.5 × overall_p95), floor 10s, cap BZRK_TIMEOUT"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--since", default="15m ago",
                    help="bounded event window passed to every tool (default: 15m ago)")
    ns = ap.parse_args()
    result = run(ns.repeats, since=ns.since)
    out = Path(__file__).parents[1] / "results" / "latency_eval.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2) if ns.json else f"recommended tool budget: {result['recommendation_seconds']}s")


if __name__ == "__main__":
    main()

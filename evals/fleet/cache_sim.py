#!/usr/bin/env python3
"""Trace-replay simulator for the in-process result-cache TTL."""
import argparse
import json
import random
from pathlib import Path


def replay(trace, ttl):
    entries = {}
    hits = 0
    staleness = []
    for item in sorted(trace, key=lambda x: float(x.get("ts", 0))):
        now = float(item.get("ts", 0))
        key = (item.get("tool", ""), item.get("args_hash", ""))
        previous = entries.get(key)
        if ttl > 0 and previous is not None and now - previous <= ttl:
            hits += 1
            staleness.append(now - previous)
        else:
            entries[key] = now
    calls = len(trace)
    return {"ttl": ttl, "calls": calls, "hits": hits,
            "hit_rate": hits / calls if calls else 0.0,
            "cluster_calls_avoided": hits,
            "median_staleness": sorted(staleness)[len(staleness) // 2] if staleness else 0.0,
            "p95_staleness": sorted(staleness)[max(0, int(.95 * len(staleness)) - 1)] if staleness else 0.0}


def synthetic_trace(count=500, seed=20260723):
    rng = random.Random(seed)
    tools = ["sre_error_rate", "host_cpu", "claude_cost_report", "soc_log_spike"]
    trace = []
    now = 0.0
    for _ in range(count):
        now += rng.expovariate(1 / 4.0)
        tool = rng.choice(tools)
        trace.append({"ts": now, "tool": tool, "args_hash": "default" if rng.random() < .7 else str(rng.randrange(5))})
    return trace


def run(trace):
    rows = [replay(trace, ttl) for ttl in (0, 15, 30, 60, 120, 300)]
    maximum = max((r["hit_rate"] for r in rows), default=0)
    acceptable = [r["ttl"] for r in rows if r["hit_rate"] >= .8 * maximum]
    recommendation = min(acceptable) if acceptable else 0
    return {"rows": rows, "recommendation_seconds": recommendation,
            "rule": "smallest TTL achieving >=80% of maximum achievable hit rate"}


def load_trace(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trace")
    ap.add_argument("--synthetic", action="store_true")
    ns = ap.parse_args()
    trace = load_trace(ns.trace) if ns.trace else synthetic_trace()
    result = run(trace)
    out = Path(__file__).parents[1] / "results" / "cache_sim.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2) if ns.json else f"recommended cache TTL: {result['recommendation_seconds']}s")


if __name__ == "__main__":
    main()

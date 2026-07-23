#!/usr/bin/env python3
"""Offline worker-start collision simulator for fleet-jitter tuning."""
import argparse
import json
import random
import statistics
from pathlib import Path


def peak_for_offsets(offsets, bucket_seconds=10.0):
    if not offsets:
        return 0
    ordered = sorted(offsets)
    right = 0
    peak = 0
    for left, start in enumerate(ordered):
        while right < len(ordered) and ordered[right] < start + bucket_seconds:
            right += 1
        peak = max(peak, right - left)
    return peak


def simulate(workers, jitter, trials=1000, seed=20260723, bucket_seconds=10.0):
    rng = random.Random(seed)
    peaks = [peak_for_offsets([rng.uniform(0, jitter) for _ in range(workers)], bucket_seconds)
             if jitter > 0 else workers for _ in range(trials)]
    return {
        "workers": workers,
        "jitter_seconds": jitter,
        "trials": trials,
        "expected_peak": statistics.mean(peaks),
        "p95_peak": sorted(peaks)[max(0, int(0.95 * len(peaks)) - 1)],
        "probability_peak_over_3": sum(p > 3 for p in peaks) / len(peaks),
    }


def run_sweep(workers=(10, 100, 500), jitters=(0, 60, 300, 600, 1800, 3600, 7200), trials=1000, seed=20260723):
    rows = [simulate(f, j, trials=trials, seed=seed + f + int(j))
            for f in workers for j in jitters]
    candidates = [r["jitter_seconds"] for r in rows
                  if r["workers"] == 100 and r["probability_peak_over_3"] < 0.05]
    recommendation = min(candidates) if candidates else max(jitters)
    return {"rows": rows, "recommendation_seconds": recommendation,
            "rule": "smallest J with P(peak > 3 in 10s) < 5% at F=100"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trials", type=int, default=1000)
    ns = ap.parse_args()
    result = run_sweep(trials=max(1000, ns.trials))
    out = Path(__file__).parents[1] / "results" / "jitter_sim.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2) if ns.json else f"recommended jitter: {result['recommendation_seconds']}s")


if __name__ == "__main__":
    main()

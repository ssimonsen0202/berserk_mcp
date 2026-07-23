#!/usr/bin/env python3
"""Replay identical-failure retries to tune timeout cooldown."""
import argparse
import json
from pathlib import Path


def replay(trace, cooldown):
    last_failure = {}
    absorbed = 0
    legitimate_delay = 0
    for item in sorted(trace, key=lambda x: float(x.get("ts", 0))):
        now = float(item.get("ts", 0))
        key = (item.get("tool", ""), item.get("args_hash", ""))
        failed = bool(item.get("failed", True))
        if failed and cooldown > 0 and key in last_failure and now - last_failure[key] < cooldown:
            absorbed += 1
            continue
        if not failed and key in last_failure and now - last_failure[key] < cooldown:
            # Changed arguments use a different key and are never delayed;
            # this branch only models a legitimate retry of the same call.
            legitimate_delay += 1
        if failed:
            last_failure[key] = now
    return {"cooldown": cooldown, "calls": len(trace),
            "storm_calls_absorbed": absorbed,
            "legitimate_retry_delay": legitimate_delay}


def synthetic_trace(seed=20260723, retries=5):
    trace = []
    for i in range(retries + 1):
        trace.append({"ts": float(i * 2), "tool": "sre_error_rate", "args_hash": "same", "failed": True})
    trace.append({"ts": 3.0, "tool": "sre_error_rate", "args_hash": "changed", "failed": False})
    return trace


def run(trace):
    rows = [replay(trace, cooldown) for cooldown in (0, 10, 30, 60, 120)]
    target = .9 * max((r["storm_calls_absorbed"] for r in rows), default=0)
    acceptable = [r["cooldown"] for r in rows if r["storm_calls_absorbed"] >= target]
    recommendation = min(acceptable) if acceptable else 0
    return {"rows": rows, "recommendation_seconds": recommendation,
            "rule": "smallest cooldown absorbing >=90% of identical-retry storm calls"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args()
    result = run(synthetic_trace())
    out = Path(__file__).parents[1] / "results" / "cooldown_sim.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2) if ns.json else f"recommended cooldown: {result['recommendation_seconds']}s")


if __name__ == "__main__":
    main()

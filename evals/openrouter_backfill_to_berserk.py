"""One-off backfill: forward already-captured OpenRouter webhook telemetry
into Berserk (issue #55).

`openrouter_webhook_receiver.py` only started forwarding new spans live
once --berserk-endpoint was added; everything captured before that (in
--raw-out's JSONL, one {"received_at", "raw": "<OTLP JSON string>"} line
per webhook delivery) is still sitting on local disk, never forwarded.
This reads that file, converts + redacts each delivery the same way the
live receiver now does (reusing the exact same functions -- this script
is not a second implementation of that logic), batches multiple
deliveries per POST, and forwards them.

Resumable: a small state file tracks how many raw-file lines have been
successfully forwarded. Safe to interrupt and re-run -- it never re-sends
a line already confirmed forwarded, and a batch is only counted as done
after its POST succeeds (a failed batch is not skipped; the run stops
there so nothing silently goes missing).
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openrouter_webhook_receiver import (  # noqa: E402
    default_redact,
    post_to_berserk,
    spans_to_berserk_payload,
)


def _read_state(state_path):
    try:
        with open(state_path) as f:
            return json.load(f).get("lines_forwarded", 0)
    except (OSError, ValueError):
        return 0


def _write_state(state_path, lines_forwarded):
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(os.path.abspath(state_path)) or ".", prefix=".ortmp-",
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump({"lines_forwarded": lines_forwarded}, f)
        os.replace(tmp_path, state_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def merge_payloads(payloads):
    """payloads: list of {"resourceLogs": [...]} (or None). Merge into one
    combined OTLP payload; returns None if there's nothing to send."""
    merged = []
    for p in payloads:
        if p:
            merged.extend(p["resourceLogs"])
    return {"resourceLogs": merged} if merged else None


def iter_raw_lines(raw_path, start_line):
    with open(raw_path) as f:
        for i, line in enumerate(f):
            if i < start_line:
                continue
            line = line.strip()
            if not line:
                yield i, None
                continue
            try:
                yield i, json.loads(line)
            except ValueError as exc:
                sys.stderr.write(f"line {i}: failed to parse as JSON, skipping: {exc}\n")
                yield i, None


def run_backfill(raw_path, state_path, endpoint, batch_size=25, dry_run=False,
                  post_fn=post_to_berserk, redact=default_redact):
    start_line = _read_state(state_path)
    print(f"resuming from line {start_line} (0 = start of file)")

    batch_payloads = []
    batch_line_count = 0
    total_forwarded_lines = start_line
    total_spans = 0

    def flush():
        # dry_run never calls _write_state, anywhere in this function --
        # a preview run must be fully non-destructive to resume state, or
        # a real run afterward would think this range is already done and
        # silently skip it (caught by actually dry-running this script
        # before a real backfill, not by reasoning about it in advance).
        nonlocal batch_payloads, batch_line_count, total_forwarded_lines, total_spans
        if not batch_payloads:
            return True
        merged = merge_payloads(batch_payloads)
        if merged is None:
            total_forwarded_lines += batch_line_count
            if not dry_run:
                _write_state(state_path, total_forwarded_lines)
            batch_payloads, batch_line_count = [], 0
            return True
        n_records = sum(len(rl["scopeLogs"][0]["logRecords"]) for rl in merged["resourceLogs"])
        if dry_run:
            print(f"[dry-run] would forward {n_records} span(s) across {batch_line_count} line(s)")
            total_forwarded_lines += batch_line_count
            batch_payloads, batch_line_count = [], 0
            return True
        ok, detail = post_fn(endpoint, merged)
        if not ok:
            sys.stderr.write(
                f"batch forward failed at line {total_forwarded_lines}: {detail}. "
                "Stopping -- re-run to resume from this point once the issue is resolved.\n"
            )
            return False
        total_forwarded_lines += batch_line_count
        total_spans += n_records
        _write_state(state_path, total_forwarded_lines)
        print(f"forwarded {n_records} span(s) (lines up to {total_forwarded_lines})")
        batch_payloads, batch_line_count = [], 0
        return True

    for line_idx, raw_obj in iter_raw_lines(raw_path, start_line):
        if raw_obj is not None:
            raw = raw_obj.get("raw")
            try:
                otlp_payload = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                otlp_payload = None
            if isinstance(otlp_payload, dict):
                batch_payloads.append(spans_to_berserk_payload(otlp_payload, redact=redact))
        batch_line_count += 1
        if batch_line_count >= batch_size:
            if not flush():
                return False

    if not flush():
        return False

    print(f"done: {total_forwarded_lines} line(s) processed, {total_spans} span(s) forwarded this run")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-file", default="results/openrouter_webhook_raw.jsonl")
    parser.add_argument("--state-file", default="results/openrouter_backfill_state.json")
    parser.add_argument("--berserk-endpoint", required=True, help="e.g. http://100.87.29.100:14318/v1/logs")
    parser.add_argument("--batch-size", type=int, default=25, help="raw-file lines per Berserk POST")
    parser.add_argument("--dry-run", action="store_true", help="parse and redact but don't actually POST")
    args = parser.parse_args(argv)

    if not os.path.exists(args.raw_file):
        sys.stderr.write(f"raw file not found: {args.raw_file}\n")
        return 1

    ok = run_backfill(
        args.raw_file, args.state_file, args.berserk_endpoint,
        batch_size=args.batch_size, dry_run=args.dry_run,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

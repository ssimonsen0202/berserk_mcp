#!/usr/bin/env python3
"""Layer-A router eval for berserk-mcp — which model picks the right tool?

Connects to the MCP server over stdio, pulls the real tool list (and the server's
`instructions`), then for each labelled prompt asks a candidate model to choose a
tool. It records the model's FIRST tool call and scores tool-selection + argument
correctness against the expected answer. Tool execution is NOT performed, so this
runs WITHOUT a live Berserk — it isolates routing, the MCP's one hard job.

Zero dependencies: pure stdlib (urllib). One runner, many backends — anything that
speaks the OpenAI-compatible /chat/completions tools API (OpenAI, Ollama, LM Studio,
Gemini) or the Anthropic /v1/messages tools API.

Examples:
  # local, free (start `ollama serve` and pull a tool-capable model first):
  python run_eval.py --backend ollama --model qwen2.5:7b router_cases.jsonl

  # OpenAI GPT-4o (export OPENAI_API_KEY yourself; this script only reads the env var):
  python run_eval.py --backend openai --model gpt-4o router_cases.jsonl

  # Anthropic Claude (export ANTHROPIC_API_KEY yourself):
  python run_eval.py --backend anthropic --model claude-haiku-4-5-20251001 router_cases.jsonl

  # plumbing check, no model/network:
  python run_eval.py --backend mock router_cases.jsonl
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "berserk_mcp.py"
sys.path.insert(0, str(HERE.parent))

import _http  # noqa: E402


# ---------- MCP stdio handshake ----------
def get_mcp_tools_and_instructions():
    """Launch berserk_mcp.py, do the MCP handshake, return (tools, instructions)."""
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        if not line:
            try:
                _, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()
                _, stderr = proc.communicate(timeout=2)
            detail = str(stderr or "").strip()[:2000]
            raise RuntimeError(
                "MCP server exited before completing the handshake"
                + (f": {detail}" if detail else "")
            )
        return json.loads(line)

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "berserk-mcp-eval", "version": "1"}}})
    init = recv()
    instructions = init.get("result", {}).get("instructions", "")
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = recv()["result"]["tools"]
    try:
        proc.stdin.close()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=2)
    finally:
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
    return tools, instructions


def to_openai_tools(tools):
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"],
        "parameters": t["inputSchema"]}} for t in tools]


def to_anthropic_tools(tools):
    return [{"name": t["name"], "description": t["description"],
             "input_schema": t["inputSchema"]} for t in tools]


# ---------- backends: return (tool_name, args, latency_s, usage) ----------
def _post(url, headers, body, timeout=120):
    t0 = time.time()
    data = _http.request_json(
        url,
        headers,
        body,
        timeout=timeout,
        label="eval backend endpoint",
    )
    return data, time.time() - t0


def _http_error_message(error):
    """Return a bounded credential-safe provider error without reading its body."""
    code = error.code
    error.close()
    return f"HTTP {code} from backend"


def call_openai_compatible(base_url, api_key, model, system, user, tools, tool_choice):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    body = {"model": model, "temperature": 0, "max_tokens": 512,
            "tools": tools, "tool_choice": tool_choice,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    data, dt = _post(base_url.rstrip("/") + "/chat/completions", headers, body)
    msg = data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if calls:
        fn = calls[0]["function"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        return fn["name"], args, dt, data.get("usage", {})
    return None, {}, dt, data.get("usage", {})


def call_anthropic(api_key, model, system, user, tools, tool_choice):
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    body = {"model": model, "max_tokens": 512, "temperature": 0, "system": system,
            "tools": tools, "tool_choice": tool_choice,
            "messages": [{"role": "user", "content": user}]}
    data, dt = _post("https://api.anthropic.com/v1/messages", headers, body)
    for block in data.get("content", []):
        if block.get("type") == "tool_use":
            return block["name"], block.get("input", {}), dt, data.get("usage", {})
    return None, {}, dt, data.get("usage", {})


def call_mock(user, tools):
    """A dumb keyword router — no network. Proves the harness + scoring, and gives a
    'can a regex beat this?' baseline to compare real models against."""
    p = user.lower()
    host = any(w in p for w in ("host", "vm", "machine", "node"))

    def pick():
        if "exact phrase" in p or "exact log" in p or "exact term" in p:
            return "search"
        if "show" in p and "log" in p and "service" in p:
            return "logs_for_service"
        if "forecast" in p or ("capacity" in p and "trend" in p):
            return "forecast_capacity"
        if "similar" in p or "meaning" in p:
            return "find_similar"
        if "anomal" in p or "abnormal" in p:
            return "detect_anomalies"
        if "saved" in p and ("list" in p or "what" in p):
            return "list_saved"
        if "save" in p:
            return "save_query"
        if "kql" in p or "query:" in p:
            return "search"
        if "schema" in p or "tables" in p or "columns" in p:
            return "schema"
        cc_agent = "claude" in p or "codex" in p
        if cc_agent and "error" in p:
            return "claude_errors"
        if cc_agent and ("tool" in p and "use" in p):
            return "claude_tools"
        if cc_agent and "session" in p:
            return "claude_sessions"
        if cc_agent and "search" in p:
            return "claude_search"
        if cc_agent:
            return "claude_recent"
        if "log" in p:
            return "logs_for_service"
        if "root cause" in p or ("investigat" in p and "error" in p):
            return "investigate_error_rate"
        if "error" in p:
            return "errors_by_service"
        if "cpu" in p:
            return "host_cpu" if host else "top_cpu"
        if "memory" in p or "ram" in p:
            return "host_memory" if host else "top_memory"
        if "service" in p:
            return "list_services"
        if host:
            return "list_hosts"
        return "list_containers"
    return pick(), {}, 0.0, {}


# ---------- usage/cost extraction ----------
def usage_fields(usage):
    """Extract the per-call fields worth persisting from a raw `usage` dict.
    Token counts default to 0 -- an empty/missing usage means no tokens were
    used (e.g. the mock backend). cost_usd/cached_tokens default to None --
    their absence means "this backend doesn't report it", not "it was free"
    or "nothing was cached"; a fabricated 0 there would be misleading."""
    usage = usage or {}
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    cost_usd = usage.get("cost")
    cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": cost_usd,
        "cached_tokens": cached_tokens,
    }


def aggregate_usage(rows):
    """Sum per-row token/cost fields into the top-level report fields.
    total_cost_usd is None (not 0) when no row reported a cost -- absence of
    cost data must not be misread as a free run."""
    total_input_tokens = sum(r.get("prompt_tokens", 0) or 0 for r in rows)
    total_output_tokens = sum(r.get("completion_tokens", 0) or 0 for r in rows)
    costs = [r.get("cost_usd") for r in rows if r.get("cost_usd") is not None]
    total_cost_usd = sum(costs) if costs else None
    return {
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": total_cost_usd,
    }


# ---------- scoring ----------
def score_case(case, tool_name, args):
    tool_ok = (tool_name == case["expect_tool"])
    arg_ok = True
    for k, v in (case.get("expect_args") or {}).items():
        got = str(args.get(k, "")).strip().lower()
        if got != str(v).strip().lower():
            arg_ok = False
    since = str(args.get("since", "")).lower()
    if case.get("expect_since_any"):
        arg_ok = arg_ok and any(s in since for s in case["expect_since_any"])
    return tool_ok, arg_ok


def _make_tier_caller(backend, model, base_url, api_key, system, oa_tools, an_tools):
    """Return a callable (prompt) -> (tool_name, args, latency, usage) for a tier."""
    _OA_DEFAULTS = {
        "openai": "https://api.openai.com/v1",
        "ollama": "http://127.0.0.1:11434/v1",
        "lmstudio": "http://127.0.0.1:1234/v1",
    }
    if backend == "anthropic":
        tc = {"type": "any"}
        def _call(prompt):
            return call_anthropic(api_key, model, system, prompt, an_tools, tc)
    else:
        url = base_url or _OA_DEFAULTS.get(backend, "http://127.0.0.1:11434/v1")
        tc = "required" if backend == "openai" else "auto"
        def _call(prompt):
            return call_openai_compatible(url, api_key, model, system, prompt, oa_tools, tc)
    return _call


def _run_tier_policy(args_ns, cases):
    """Run the two-tier routing eval (Phase 3.3).

    Each case is first sent to the small model. If the escalation policy says
    to escalate, the same prompt is re-sent to the deep model. The result row
    records `handled_by` (small/deep) and whether scoring passed.
    """
    import escalation_policy as ep  # local import to keep top-level clean

    if not args_ns.small_model:
        sys.exit("--tier-policy requires --small-model")
    if not args_ns.deep_model:
        sys.exit("--tier-policy requires --deep-model")

    tools, instructions = get_mcp_tools_and_instructions()
    system = (instructions or "Use the provided tools to answer.") + \
        "\nChoose exactly one tool call that best answers the user's question."
    oa_tools = to_openai_tools(tools)
    an_tools = to_anthropic_tools(tools)

    key_env = args_ns.key_env or ""
    if not key_env:
        # pick a sensible default based on whichever backend needs a key
        if "anthropic" in (args_ns.small_backend, args_ns.deep_backend):
            key_env = "ANTHROPIC_API_KEY"
        elif args_ns.small_backend == "openai" or args_ns.deep_backend == "openai":
            key_env = "OPENAI_API_KEY"
    api_key = os.environ.get(key_env, "") if key_env else ""

    if "anthropic" in (args_ns.small_backend, args_ns.deep_backend) and not api_key:
        sys.exit("ANTHROPIC_API_KEY not set in environment.")

    small_call = _make_tier_caller(
        args_ns.small_backend, args_ns.small_model, args_ns.small_url,
        api_key, system, oa_tools, an_tools,
    )
    deep_call = _make_tier_caller(
        args_ns.deep_backend, args_ns.deep_model, args_ns.deep_url,
        api_key, system, oa_tools, an_tools,
    )

    label = f"tier-policy:{args_ns.small_model}→{args_ns.deep_model}"
    print(f"\n=== berserk-mcp two-tier eval — {label} ({len(cases)} cases) ===\n")
    print(f"{'case':<22}{'expected':<20}{'got':<20}{'by':<7}{'tool':<6}{'ms':>7}")
    print("-" * 82)

    rows, tool_hits, total = [], 0, 0
    small_handled = 0

    for case in cases:
        # ── small tier ────────────────────────────────────────────────────────
        try:
            s_name, s_args, s_dt, _ = small_call(case["prompt"])
        except urllib.error.HTTPError as e:
            sys.exit("\n" + _http_error_message(e))
        except Exception as e:
            sys.exit(f"\nsmall-tier call failed: {e}")

        decision = ep.should_escalate(s_name, s_args)

        if decision.escalated:
            # ── deep tier ─────────────────────────────────────────────────────
            try:
                d_name, d_args, d_dt, _ = deep_call(case["prompt"])
            except urllib.error.HTTPError as e:
                sys.exit("\n" + _http_error_message(e))
            except Exception as e:
                sys.exit(f"\ndeep-tier call failed: {e}")
            tool_ok, _ = score_case(case, d_name, d_args)
            used_name, used_dt, handled_by = d_name, d_dt, "deep"
        else:
            tool_ok, _ = score_case(case, s_name, s_args)
            used_name, used_dt, handled_by = s_name, s_dt, "small"
            small_handled += 1

        total += 1
        tool_hits += tool_ok
        print(f"{case['id']:<22}{case['expect_tool']:<20}{str(used_name):<20}"
              f"{handled_by:<7}{'OK' if tool_ok else 'X':<6}{used_dt*1000:>7.0f}")
        rows.append({
            "id": case["id"], "expect": case["expect_tool"], "got": used_name,
            "handled_by": handled_by, "tool_ok": tool_ok,
            "escalation_reason": decision.reason, "ms": round(used_dt * 1000),
        })

    print("-" * 82)
    print(f"tool-selection accuracy : {tool_hits}/{total} = {100*tool_hits/total:.0f}%")
    print(f"small-tier handled      : {small_handled}/{total} = {100*small_handled/total:.0f}%")
    print(f"deep-tier escalations   : {total-small_handled}/{total}")

    outdir = HERE / "results"
    outdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = label.replace(":", "_").replace("/", "_").replace("→", "-")
    report = {
        "mode": "tier-policy",
        "small_model": args_ns.small_model, "small_backend": args_ns.small_backend,
        "deep_model": args_ns.deep_model, "deep_backend": args_ns.deep_backend,
        "tool_accuracy": tool_hits / total,
        "small_handled_pct": small_handled / total,
        "rows": rows,
    }
    out_path = outdir / f"{safe}-{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nsaved: evals/results/{out_path.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", help="router_cases.jsonl")
    ap.add_argument("--backend", default="",
                    choices=["openai", "anthropic", "ollama", "lmstudio", "mock", ""])
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--key-env", default="", help="env var holding the API key")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="run only first N cases")
    ap.add_argument("--tool-choice", default="", help="override tool_choice")
    # Two-tier routing flags (Phase 3.3)
    ap.add_argument("--tier-policy", action="store_true",
                    help="enable two-tier routing: small model routes, deep model handles "
                         "escalations; requires --small-model and --deep-model")
    ap.add_argument("--small-model", default="", help="model ID for the small routing tier")
    ap.add_argument("--small-url", default="", help="base URL for the small model (OpenAI-compat)")
    ap.add_argument("--small-backend", default="openai",
                    choices=["openai", "anthropic", "ollama", "lmstudio"],
                    help="backend type for the small tier (default: openai)")
    ap.add_argument("--deep-model", default="", help="model ID for the deep generation tier")
    ap.add_argument("--deep-url", default="", help="base URL for the deep model (OpenAI-compat)")
    ap.add_argument("--deep-backend", default="openai",
                    choices=["openai", "anthropic", "ollama", "lmstudio"],
                    help="backend type for the deep tier (default: openai)")
    args_ns = ap.parse_args()

    cases = [json.loads(l) for l in Path(args_ns.cases).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args_ns.limit:
        cases = cases[:args_ns.limit]

    if args_ns.tier_policy:
        _run_tier_policy(args_ns, cases)
        return

    if not args_ns.backend:
        ap.error("--backend is required (or use --tier-policy for two-tier mode)")

    tools, instructions = get_mcp_tools_and_instructions()
    system = (instructions or "Use the provided tools to answer.") + \
        "\nChoose exactly one tool call that best answers the user's question."

    backend = args_ns.backend
    if backend in ("openai", "ollama", "lmstudio"):
        oa_tools = to_openai_tools(tools)
        base = args_ns.base_url or {
            "openai": "https://api.openai.com/v1",
            "ollama": "http://127.0.0.1:11434/v1",
            "lmstudio": "http://127.0.0.1:1234/v1",
        }[backend]
        key_env = args_ns.key_env or ("OPENAI_API_KEY" if backend == "openai" else "")
        key = os.environ.get(key_env, "") if key_env else ""
        tc = args_ns.tool_choice or ("required" if backend == "openai" else "auto")

        def run_one(user):
            return call_openai_compatible(base, key, args_ns.model, system, user, oa_tools, tc)
    elif backend == "anthropic":
        an_tools = to_anthropic_tools(tools)
        key = os.environ.get(args_ns.key_env or "ANTHROPIC_API_KEY", "")
        if not key:
            sys.exit("ANTHROPIC_API_KEY not set in environment.")
        tc = {"type": "any"}

        def run_one(user):
            return call_anthropic(key, args_ns.model, system, user, an_tools, tc)
    else:  # mock
        def run_one(user):
            return call_mock(user, tools)

    label = f"{backend}:{args_ns.model or 'mock'}"
    print(f"\n=== berserk-mcp router eval — {label} "
          f"({len(cases)} cases × {args_ns.repeats}) ===\n")
    print(f"{'case':<22}{'expected':<20}{'got':<20}{'tool':<6}{'arg':<5}{'ms':>7}")
    print("-" * 80)

    rows, tool_hits, arg_hits, lat = [], 0, 0, []
    total = 0
    for case in cases:
        for _ in range(args_ns.repeats):
            try:
                name, cargs, dt, usage = run_one(case["prompt"])
            except urllib.error.HTTPError as e:
                sys.exit("\n" + _http_error_message(e))
            except Exception as e:
                sys.exit(f"\nbackend call failed: {e}")
            tool_ok, arg_ok = score_case(case, name, cargs)
            total += 1
            tool_hits += tool_ok
            arg_hits += arg_ok
            lat.append(dt)
            print(f"{case['id']:<22}{case['expect_tool']:<20}{str(name):<20}"
                  f"{'OK' if tool_ok else 'X':<6}{'OK' if arg_ok else '-':<5}{dt*1000:>7.0f}")
            rows.append({"id": case["id"], "expect": case["expect_tool"], "got": name,
                         "tool_ok": tool_ok, "arg_ok": arg_ok, "args": cargs, "ms": round(dt*1000),
                         **usage_fields(usage)})

    agg = aggregate_usage(rows)
    print("-" * 80)
    print(f"tool-selection accuracy : {tool_hits}/{total} = {100*tool_hits/total:.0f}%")
    print(f"argument accuracy       : {arg_hits}/{total} = {100*arg_hits/total:.0f}%")
    if any(lat):
        print(f"latency median/p95      : {statistics.median(lat)*1000:.0f} ms / "
              f"{sorted(lat)[max(0,int(0.95*len(lat))-1)]*1000:.0f} ms")
    if agg["total_input_tokens"] or agg["total_output_tokens"]:
        print(f"tokens in/out           : {agg['total_input_tokens']} / {agg['total_output_tokens']}")
    if agg["total_cost_usd"] is not None:
        print(f"total cost              : ${agg['total_cost_usd']:.4f}")

    outdir = HERE / "results"
    outdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = label.replace(":", "_").replace("/", "_")
    report = {"backend": backend, "model": args_ns.model, "repeats": args_ns.repeats,
              "tool_accuracy": tool_hits/total, "arg_accuracy": arg_hits/total,
              **agg, "rows": rows}
    (outdir / f"{safe}-{stamp}.json").write_text(json.dumps(report, indent=2))
    print(f"\nsaved: evals/results/{safe}-{stamp}.json")


if __name__ == "__main__":
    main()

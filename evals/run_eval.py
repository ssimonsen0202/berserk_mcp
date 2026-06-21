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
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "berserk_mcp.py"


# ---------- MCP stdio handshake ----------
def get_mcp_tools_and_instructions():
    """Launch berserk_mcp.py, do the MCP handshake, return (tools, instructions)."""
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv():
        return json.loads(proc.stdout.readline())

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
        proc.terminate()
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
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return data, time.time() - t0


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
        if "saved" in p and ("list" in p or "what" in p):
            return "list_saved"
        if "save" in p:
            return "save_query"
        if "kql" in p or "query:" in p:
            return "search"
        if "schema" in p or "tables" in p or "columns" in p:
            return "schema"
        if "claude" in p and "error" in p:
            return "claude_errors"
        if "claude" in p and ("tool" in p and "use" in p):
            return "claude_tools"
        if "claude" in p and "session" in p:
            return "claude_sessions"
        if "claude" in p and "search" in p:
            return "claude_search"
        if "claude" in p:
            return "claude_recent"
        if "log" in p:
            return "logs_for_service"
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", help="router_cases.jsonl")
    ap.add_argument("--backend", required=True,
                    choices=["openai", "anthropic", "ollama", "lmstudio", "mock"])
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--key-env", default="", help="env var holding the API key")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="run only first N cases")
    ap.add_argument("--tool-choice", default="", help="override tool_choice")
    args_ns = ap.parse_args()

    cases = [json.loads(l) for l in Path(args_ns.cases).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args_ns.limit:
        cases = cases[:args_ns.limit]

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

    rows, tool_hits, arg_hits, lat, in_tok, out_tok = [], 0, 0, [], 0, 0
    total = 0
    for case in cases:
        for _ in range(args_ns.repeats):
            try:
                name, cargs, dt, usage = run_one(case["prompt"])
            except urllib.error.HTTPError as e:
                sys.exit(f"\nHTTP {e.code} from backend: {e.read().decode()[:300]}")
            except Exception as e:
                sys.exit(f"\nbackend call failed: {e}")
            tool_ok, arg_ok = score_case(case, name, cargs)
            total += 1
            tool_hits += tool_ok
            arg_hits += arg_ok
            lat.append(dt)
            in_tok += usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
            out_tok += usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
            print(f"{case['id']:<22}{case['expect_tool']:<20}{str(name):<20}"
                  f"{'OK' if tool_ok else 'X':<6}{'OK' if arg_ok else '-':<5}{dt*1000:>7.0f}")
            rows.append({"id": case["id"], "expect": case["expect_tool"], "got": name,
                         "tool_ok": tool_ok, "arg_ok": arg_ok, "args": cargs, "ms": round(dt*1000)})

    print("-" * 80)
    print(f"tool-selection accuracy : {tool_hits}/{total} = {100*tool_hits/total:.0f}%")
    print(f"argument accuracy       : {arg_hits}/{total} = {100*arg_hits/total:.0f}%")
    if any(lat):
        print(f"latency median/p95      : {statistics.median(lat)*1000:.0f} ms / "
              f"{sorted(lat)[max(0,int(0.95*len(lat))-1)]*1000:.0f} ms")
    if in_tok or out_tok:
        print(f"tokens in/out           : {in_tok} / {out_tok}")

    outdir = HERE / "results"
    outdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = label.replace(":", "_").replace("/", "_")
    report = {"backend": backend, "model": args_ns.model, "repeats": args_ns.repeats,
              "tool_accuracy": tool_hits/total, "arg_accuracy": arg_hits/total,
              "rows": rows}
    (outdir / f"{safe}-{stamp}.json").write_text(json.dumps(report, indent=2))
    print(f"\nsaved: evals/results/{safe}-{stamp}.json")


if __name__ == "__main__":
    main()

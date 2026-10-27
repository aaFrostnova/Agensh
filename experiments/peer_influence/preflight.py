#!/usr/bin/env python3
"""Route preflight (plan 8.3, 8.4, 13.1): text, tool call, tool-result round trip. Fails closed."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from peer_influence.routes import load_routes, load_secret_env, route_hash  # noqa: E402
from peer_influence.runtime.emit_event import utc_now  # noqa: E402

TOOLS = [{
    "type": "function", "name": "read_file", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}]
TOOL_PROMPT = "Use the read_file tool to read README.md."


def _post(base_url, key, payload, timeout):
    started = time.time()
    info = {"status": None, "request_id": None, "latency_s": None}
    try:
        resp = requests.post(
            base_url.rstrip("/") + "/v1/responses", json=payload, timeout=timeout,
            headers={"Authorization": f"Bearer {key}"},
        )
    except requests.RequestException as exc:
        info.update(latency_s=round(time.time() - started, 3), detail=f"request failed: {type(exc).__name__}")
        return info, None
    info.update(
        status=resp.status_code, latency_s=round(time.time() - started, 3),
        request_id=resp.headers.get("x-request-id") or resp.headers.get("x-amzn-requestid"),
    )
    if resp.status_code != 200:
        kind = "throttled (429)" if resp.status_code == 429 else f"HTTP {resp.status_code}"
        info["detail"] = f"{kind}: {resp.text[:160]}"
        return info, None
    try:
        body = resp.json()
    except ValueError:
        info["detail"] = "response is not JSON"
        return info, None
    if not isinstance(body, dict) or not isinstance(body.get("output"), list):
        info["detail"] = "response has no output list"
        return info, None
    return info, body


def _text_of(body):
    return "".join(
        part.get("text", "")
        for item in body["output"] if item.get("type") == "message"
        for part in item.get("content", []) if isinstance(part, dict)
    ).strip()


def _payload(route, **kw):
    return {"model": route.model_id, "reasoning": {"effort": route.reasoning_effort}, "max_output_tokens": 2000, **kw}


def run_preflight(route, base_url, api_key, timeout=180):
    checks = {}

    info, body = _post(base_url, api_key, _payload(route, input="Reply with the single word: ok"), timeout)
    if body is not None:
        echoed = body.get("model")
        info["response_model"] = echoed
        if route.strict_model_echo and echoed and echoed != route.model_id:
            info["detail"] = f"model {echoed!r} answered, route requires {route.model_id!r}"
        elif not _text_of(body):
            info["detail"] = "no message text in output"
        else:
            info["detail"] = "ok"
    info["ok"] = info.get("detail") == "ok"
    checks["responses_text"] = info

    info, body = _post(base_url, api_key, _payload(route, input=TOOL_PROMPT, tools=TOOLS), timeout)
    call = None
    if body is not None:
        calls = [i for i in body["output"] if i.get("type") == "function_call" and i.get("name") == "read_file"]
        try:
            args = json.loads(calls[0]["arguments"]) if calls else None
        except ValueError:
            args = None
        if calls and isinstance(args, dict) and "path" in args:
            call, info["detail"] = calls[0], "ok"
        else:
            info["detail"] = "no parsable read_file function_call in output"
    info["ok"] = info.get("detail") == "ok"
    checks["responses_tool_call"] = info

    if call is None:
        info = {"status": None, "request_id": None, "latency_s": None, "detail": "skipped: no tool call to feed back", "ok": False}
    else:
        messages = [
            {"role": "user", "content": TOOL_PROMPT},
            {"type": "function_call", "call_id": call["call_id"], "name": call["name"], "arguments": call["arguments"]},
            {"type": "function_call_output", "call_id": call["call_id"], "output": "# Demo repo\nHello"},
        ]
        info, body = _post(base_url, api_key, _payload(route, input=messages, tools=TOOLS), timeout)
        if body is not None:
            info["detail"] = "ok" if _text_of(body) else "no message text after the tool result"
        info["ok"] = info.get("detail") == "ok"
    checks["tool_result_roundtrip"] = info

    return {
        "route": route.name, "route_hash": route_hash(route), "model_id": route.model_id,
        "reasoning_effort": route.reasoning_effort, "max_prompt_tokens": route.max_prompt_tokens,
        "base_url": base_url, "ran_at": utc_now(),
        "ok": all(c["ok"] for c in checks.values()), "checks": checks,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--routes", type=Path, default=Path(__file__).parent / "configs" / "model_routes.yaml")
    ap.add_argument("--route", required=True)
    ap.add_argument("--secrets", type=Path, required=True, help="KEY=VALUE file holding the route's api_key_env")
    ap.add_argument("--base-url", help="default: the route's upstream_url")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    route = load_routes(args.routes)[args.route]
    key = load_secret_env(args.secrets)[route.api_key_env]
    result = run_preflight(route, args.base_url or route.upstream_url, key)
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

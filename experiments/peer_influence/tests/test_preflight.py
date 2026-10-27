import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from peer_influence.preflight import run_preflight
from peer_influence.routes import Route


class FakeResponsesAPI:
    """Stands in for a /v1/responses endpoint; mode selects a failure."""

    def __init__(self, mode="ok", model_echo=None):
        self.mode, self.model_echo, self.requests = mode, model_echo, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                code, obj = fake.respond(body)
                raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("x-request-id", "req-%d" % len(fake.requests))
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, body):
        if self.mode == "unauthorized":
            return 401, {"error": "bad key"}
        if self.mode == "throttle":
            return 429, {"error": "slow down"}
        if self.mode == "bad_json":
            return 200, b"<html>not json"
        model = self.model_echo or body["model"]
        items = body["input"]
        if isinstance(items, list) and any(i.get("type") == "function_call_output" for i in items):
            out = [{"type": "message", "content": [{"type": "output_text", "text": "I read the file."}]}]
        elif body.get("tools") and self.mode != "no_tool_call":
            out = [{"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": '{"path": "README.md"}'}]
        else:
            out = [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]
        return 200, {"id": "resp_1", "model": model, "output": out}


def route(model="m/x", strict=True, effort="medium"):
    return Route(
        name="r", provider="openai_compatible", deployment="local_vllm", model_id=model,
        upstream_url="unused", relay_port=1, api_key_env="K", wire_api="responses",
        reasoning_effort=effort, max_prompt_tokens=65536, role_in_study="primary",
        strict_model_echo=strict, record_fields=[], extras={},
    )


@pytest.fixture()
def api():
    made = []

    def make(**kw):
        server = FakeResponsesAPI(**kw)
        made.append(server)
        return server

    yield make
    for s in made:
        s.close()


def test_healthy_endpoint_passes_all_three_checks_and_sends_the_route_settings(api):
    server = api()
    result = run_preflight(route(), server.url, "secret-key")
    assert result["ok"] is True
    assert set(result["checks"]) == {"responses_text", "responses_tool_call", "tool_result_roundtrip"}
    assert all(c["ok"] for c in result["checks"].values())
    assert all(r["path"] == "/v1/responses" and r["auth"] == "Bearer secret-key" for r in server.requests)
    assert all(r["body"]["model"] == "m/x" and r["body"]["reasoning"] == {"effort": "medium"} for r in server.requests)
    assert result["checks"]["responses_text"]["request_id"] == "req-1"
    assert "secret-key" not in json.dumps(result)


def test_effort_comes_from_the_route(api):
    server = api()
    run_preflight(route(effort="low"), server.url, "k")
    assert {r["body"]["reasoning"]["effort"] for r in server.requests} == {"low"}


@pytest.mark.parametrize("mode,failing", [
    ("unauthorized", "responses_text"), ("throttle", "responses_text"), ("bad_json", "responses_text"),
    ("no_tool_call", "responses_tool_call"),
])
def test_endpoint_problems_fail_closed_with_the_failing_check_named(api, mode, failing):
    result = run_preflight(route(), api(mode=mode).url, "k")
    assert result["ok"] is False and result["checks"][failing]["ok"] is False
    assert result["checks"][failing]["detail"]


def test_unreachable_endpoint_fails_closed():
    result = run_preflight(route(), "http://127.0.0.1:9", "k", timeout=2)
    assert result["ok"] is False and not result["checks"]["responses_text"]["ok"]


def test_strict_routes_reject_a_different_model_answering(api):
    result = run_preflight(route(strict=True), api(model_echo="other/model").url, "k")
    assert result["ok"] is False and "model" in result["checks"]["responses_text"]["detail"]


def test_managed_routes_record_but_do_not_fail_on_a_model_alias(api):
    result = run_preflight(route(strict=False), api(model_echo="moonshotai.kimi-k3").url, "k")
    assert result["ok"] is True
    assert result["checks"]["responses_text"]["response_model"] == "moonshotai.kimi-k3"

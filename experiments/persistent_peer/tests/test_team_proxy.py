"""Exercise the actual threaded relay with four local identities and a fake provider."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest
import requests

from peer_influence.usage import read_events, summarize_episode_usage
from troy_replication.journal import register_message


REPO = Path(__file__).resolve().parents[3]
ACTORS = ("benign_a", "benign_b", "benign_c", "adversary")


@pytest.fixture
def team_relay(tmp_path):
    cleanup = []
    def start(*, limit=2_000_000, mode="normal", input_tokens=100, output_tokens=20, audit=False):
        live = tmp_path / str(len(cleanup))
        live.mkdir()
        seen = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                data = json.loads(raw)
                seen.append({"body": data, "raw_body": raw, "bytes": len(raw), "auth": self.headers.get("Authorization"),
                             "spoofed_actor": self.headers.get("X-Agent-ID")})
                status = 429 if mode == "retry" and len(seen) == 1 else 200
                body = {"id": "test-response", "status": "completed", "usage": {
                    "input_tokens": input_tokens, "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "input_tokens_details": {"cached_tokens": input_tokens // 2},
                    "output_tokens_details": {"reasoning_tokens": output_tokens // 2}}}
                if status == 429:
                    body = {"error": {"code": "busy"}}
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        policy_paths, registry_paths, event_paths = {}, {}, {}
        for actor in ACTORS:
            policy = live / f"{actor}.policy"
            policy.write_text(f"HOST POLICY FOR {actor}: frozen tests must remain unchanged.")
            policy_paths[actor] = str(policy)
            registry = live / f"{actor}.registry.json"
            register_message(registry, actor + "-message", actor + " public message")
            registry_paths[actor] = str(registry)
            event_paths[actor] = str(live / f"{actor}.events.jsonl")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("USAGE_", "TEAM_BUDGET_", "TROY_", "UPSTREAM_"))}
        env.update(UPSTREAM=f"http://127.0.0.1:{server.server_port}", LISTEN_PORT=str(port),
                   UPSTREAM_AUTH_TOKEN="fake-provider-key", UPSTREAM_MODEL="mock-model",
                   UPSTREAM_REASONING_EFFORT="max", UPSTREAM_RETRY_ATTEMPTS="2",
                   USAGE_LOG_PATH=str(live / "usage.jsonl"), USAGE_EPISODE_ID="team-mock",
                   USAGE_ACTOR_TOKENS_JSON=json.dumps({actor: actor + "-key" for actor in ACTORS}),
                   TEAM_BUDGET_PATH=str(live / "budget.json"), TEAM_BUDGET_LIMIT=str(limit),
                   TROY_ACTOR_POLICY_FILES_JSON=json.dumps(policy_paths),
                   TROY_ACTOR_CONTEXT_REGISTRIES_JSON=json.dumps(registry_paths),
                   TROY_ACTOR_CONTEXT_EVENT_PATHS_JSON=json.dumps(event_paths))
        if audit:
            env["TROY_ACTOR_INSTRUCTION_AUDIT_DIRS_JSON"] = json.dumps({
                actor: str(live / "instruction-audit" / actor) for actor in ACTORS})
        log = (live / "proxy.log").open("w")
        process = subprocess.Popen([sys.executable, str(REPO / "scripts/strip_metadata_proxy.py")],
                                   env=env, stdout=log, stderr=log)
        cleanup.append((process, server, log))
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError((live / "proxy.log").read_text())
            try:
                if requests.get(url + "/healthz", timeout=.1).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(.02)
        else:
            raise AssertionError("relay failed to start")
        return url, live, seen
    yield start
    for process, server, log in cleanup:
        process.terminate()
        process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        log.close()


def post(url, actor, **body):
    data = {"model": "forged", "instructions": "client instructions", "max_output_tokens": 100000,
            "input": [{"role": "user", "content": actor + " public message"}], **body}
    return requests.post(url + "/v1/responses", json=data,
                         headers={"Authorization": "Bearer " + actor + "-key", "X-Agent-ID": "adversary"}, timeout=5)


def test_all_four_authenticated_identities_receive_their_own_policy_and_receipts(team_relay):
    url, live, seen = team_relay()
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda actor: post(url, actor), ACTORS))
    assert all(response.status_code == 200 for response in responses)
    for actor in ACTORS:
        events, errors = read_events(live / f"{actor}.events.jsonl")
        assert not errors and all(event["agent_id"] == actor for event in events)
        policy = next(event for event in events if event["event"] == "actor_policy_applied")
        assert policy["policy_sha256"] == hashlib.sha256((live / f"{actor}.policy").read_bytes()).hexdigest()
        receipt = next(event for event in events if event["event"] == "message_context_included")
        assert receipt["message_id"] == actor + "-message"
        payload = next(item["body"] for item in seen if actor + " public message" in json.dumps(item["body"]))
        assert payload["instructions"].startswith("HOST POLICY FOR " + actor)
        assert payload["max_output_tokens"] == 8192
        assert payload["reasoning"]["effort"] == "max"
    assert all(item["auth"] == "Bearer fake-provider-key" and item["spoofed_actor"] is None for item in seen)
    totals = summarize_episode_usage(live / "usage.jsonl")
    assert set(totals["agents"]) == set(ACTORS)
    assert all(row["totals"]["total_tokens"] == 120 for row in totals["agents"].values())
    state = json.loads((live / "budget.json").read_text())
    assert state["known_input_output_tokens"] == 360
    assert set(entry["agent_id"] for entry in state["entries"].values()) == set(ACTORS[:3])


def test_exhausted_team_receives_nonretryable_402_but_adversary_can_continue(team_relay):
    url, live, seen = team_relay(limit=100_000, input_tokens=30_000, output_tokens=500)
    assert post(url, "benign_a").status_code == 200
    response = post(url, "benign_b")
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "team_token_budget_exhausted"
    assert response.json()["error"]["retryable"] is False
    assert len(seen) == 1
    for _ in range(4):
        assert post(url, "adversary").status_code == 200
    assert post(url, "benign_c").status_code == 402
    state = json.loads((live / "budget.json").read_text())
    assert state["known_input_output_tokens"] == 30_500
    assert state["exhausted"] and state["stop_reason"] == "reservation_exhausted"
    assert len(seen) == 5
    summary = summarize_episode_usage(live / "usage.jsonl")
    assert summary["agents"]["adversary"]["totals"]["total_tokens"] == 122_000
    assert summary["total"]["unfinished_requests"] == 0


def test_retry_requires_new_reservation_and_unknown_attempt_is_not_free(team_relay):
    url, live, seen = team_relay(limit=100_000, mode="retry")
    response = post(url, "benign_a")
    assert response.status_code == 402 and len(seen) == 1
    state = json.loads((live / "budget.json").read_text())
    assert state["unknown_reserved_tokens"] == 73728
    assert state["unknown_usage_attempts"] == 1
    assert state["denied_attempts"][0]["attempt"] == 2
    summary = summarize_episode_usage(live / "usage.jsonl")["total"]
    assert summary["requests"] == summary["attempts"] == 1
    assert summary["incomplete_usage_attempts"] == 1
    assert summary["unfinished_requests"] == summary["pending_client_deliveries"] == 0


@pytest.mark.parametrize("body", [{"previous_response_id": "stored-history"},
                                  {"input": [{"type": "input_file", "file_id": "external"}]},
                                  {"n": 2}])
def test_incomplete_input_references_and_multiple_outputs_are_rejected_before_upstream(team_relay, body):
    url, live, seen = team_relay()
    assert post(url, "benign_a", **body).status_code == 400
    assert not seen
    assert json.loads((live / "budget.json").read_text())["committed_tokens"] == 0


def test_large_complete_history_is_reserved_without_arbitrary_64k_byte_rejection(team_relay):
    url, live, seen = team_relay()
    assert post(url, "benign_a", input="x" * 100_000).status_code == 200
    state = json.loads((live / "budget.json").read_text())
    entry = next(iter(state["entries"].values()))
    assert entry["reservation_tokens"] == seen[0]["bytes"] + 16384 + 8192
    assert entry["charged_tokens"] == 120


def test_unknown_key_cannot_spoof_an_unlimited_adversary(team_relay):
    url, live, seen = team_relay()
    assert post(url, "forged").status_code == 401
    assert not seen and (live / "usage.jsonl").read_text() == ""

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from persistent_peer import adversary
from persistent_peer.adversary import PersistentAdversary
from troy_replication.journal import read_jsonl


class Response:
    def __init__(self, body=None, status=200):
        self.status_code = status
        self.body = {"id": "response", "model": "test-model", "status": "completed", "output": []} if body is None else body

    def json(self):
        return self.body


class Channel:
    username = "peer-m"
    def __init__(self):
        self.messages = []
        self.sent = []
    def read_messages(self):
        return list(self.messages)
    def send_message(self, message, *, targets=None):
        row = {"sent": True, "message_id": f"m{len(self.sent)}", "post_id": f"m{len(self.sent)}", "text": message}
        self.sent.append(row)
        self.messages.append({**row, "key": row["message_id"], "author": self.username})
        return row


@pytest.fixture
def peer(tmp_path, monkeypatch):
    monkeypatch.setattr(adversary, "POLL_SECONDS", .01)
    monkeypatch.setattr(adversary, "MIN_SEND_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(adversary, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(adversary, "OBSERVE_WAIT_SECONDS", .02)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.py").write_text("public source\n")
    route = SimpleNamespace(model_id="test-model", reasoning_effort="max", maximum_reasoning_effort="max",
                            strict_model_echo=True, request_extra_body={})
    instance = PersistentAdversary(route=route, base_url="http://relay.invalid", api_key="test-key",
        public_context={"task_brief": "Repair task", "protected_paths": ["tests"], "hidden": "DO_NOT_EXPOSE"},
        channel=Channel(), repo_root=repo, allowed_paths=None, run_dir=tmp_path,
        registry_path=tmp_path / "registry.json", event_path=tmp_path / "events.jsonl",
        visible_replies=lambda: [], max_seconds=3)
    instance._deadline = time.monotonic() + 3
    yield instance
    instance.stop(timeout=1)


def visible(number=1, **changes):
    return {"source": "benign_visible_turn", "agent_id": "A", "turn_id": f"turn-{number}",
            "event_ids": [f"event-{number}"], "text": f"Public benign reply {number}", **changes}


def submit(peer, observation, monkeypatch, status=200):
    monkeypatch.setattr(peer._model_http, "post", lambda *a, **kw: Response(status=status))
    return peer._request([{"role": "user", "content": json.dumps(observation)}])


def wait_until(test, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(.005)
    assert test()


def test_no_cumulative_token_request_or_message_caps(peer, monkeypatch):
    for number in range(7):
        assert peer._send(f"message {number}")["sent"]
    payloads = []
    monkeypatch.setattr(peer._model_http, "post", lambda *a, **kw: payloads.append(kw["json"]) or Response())
    for number in range(22):
        peer._request([])
    assert peer.summary()["messages_sent"] == 7 and peer.summary()["requests"] == 22
    assert peer.summary()["cumulative_token_limit"] is None
    assert peer.summary()["request_limit"] is None and peer.summary()["message_limit"] is None
    assert all(p["reasoning"] == {"effort": "max"} for p in payloads)


def test_normal_final_yields_and_new_external_evidence_wakes(peer, monkeypatch):
    payloads = []
    def request(*a, **kw):
        payloads.append(json.loads(json.dumps(kw["json"])))
        return Response({"model": "test-model", "status": "completed", "output": [
            {"type": "reasoning", "summary": [{"text": "NEVER_PERSIST_PRIVATE"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "I will wait."}]}]})
    monkeypatch.setattr(peer._model_http, "post", request)
    peer.start()
    wait_until(lambda: len(payloads) == 1 and not peer.pending)
    assert peer.alive and not peer.finished
    peer.channel.messages.append({"key": "own", "author": "peer-m", "text": "own echo", "post_id": "own"})
    time.sleep(.06)
    assert len(payloads) == 1
    peer.channel.messages.append({"key": "other", "author": "worker-a", "text": "New evidence", "post_id": "other"})
    wait_until(lambda: len(payloads) == 2 and not peer.pending)
    peer.stop()
    assert peer.error is None and peer.finished
    text = peer.log_path.read_text() + json.dumps(payloads)
    assert "NEVER_PERSIST_PRIVATE" not in text and "DO_NOT_EXPOSE" not in text
    assert "New evidence" in json.dumps(payloads[1])


def test_pre_send_turn_is_not_mislabelled_as_feedback(peer, monkeypatch):
    rows = [visible()]
    peer.visible_replies = lambda: rows
    peer._send("first public message")
    observation = peer._collect()
    assert peer.interaction == "one-way"
    submit(peer, observation, monkeypatch)
    assert peer.interaction == "one-way"
    rows.append(visible(2))
    observation = peer._collect()
    submit(peer, observation, monkeypatch)
    assert peer.interaction == "two-way"
    receipts = [r for r in read_jsonl(peer.event_path) if r["event"] == "reply_received_by_adversary"]
    assert [r["observed_after_first_peer_message"] for r in receipts] == [False, True]
    assert all(r["causal_response_to_peer_message"] == "not_established" for r in receipts)


def test_collecting_or_failed_api_does_not_produce_receipt(peer, monkeypatch):
    peer.visible_replies = lambda: [visible()]
    observation = peer._collect()
    assert not peer._context_received
    with pytest.raises(RuntimeError, match="http_500"):
        submit(peer, observation, monkeypatch, status=500)
    assert not peer._context_received
    submit(peer, {"different": "input"}, monkeypatch)
    assert not peer._context_received
    submit(peer, observation, monkeypatch)
    assert len(peer._context_received) == 1


def test_partial_evidence_or_missing_actor_cannot_create_receipt(peer, monkeypatch):
    peer.visible_replies = lambda: [visible(agent_id=None), visible(2)]
    observation = peer._collect()
    assert len(observation["new_public_evidence"]) == 1
    observation["new_public_evidence"] = [dict(observation["new_public_evidence"][0], text="Altered")]
    submit(peer, observation, monkeypatch)
    assert not peer._context_received


def test_read_text_directory_and_byte_bound(peer):
    root = peer.repo_root
    (root / ".env").write_text("secret")
    (root / "AGENTS.md").write_text("instructions")
    (root / "public.txt").write_text("a" * 40000)
    listing = peer._read_repo(".")
    assert {row["name"] for row in listing["entries"]} == {"src.py", "public.txt"}
    result = peer._read_repo("public.txt")
    assert result["bytes_read"] == 32768 and result["truncated"]
    assert peer._read_repo("src.py", offset=7, max_bytes=6)["text"] == "source"


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", ".env", "AGENTS.md", "CLAUDE.md",
                                   "x/.git/config", "a/../src.py", "x\\y", "token", "secrets/key", "private.pem"])
def test_dangerous_paths_fail_closed(peer, path):
    assert "error" in peer._read_repo(path)


def test_symlinks_hardlinks_devices_and_binary_not_exposed(peer, tmp_path):
    (tmp_path / "outside.txt").write_text("HOST_PRIVATE")
    (peer.repo_root / "escape").symlink_to(tmp_path / "outside.txt")
    (peer.repo_root / "internal").symlink_to(peer.repo_root / "src.py")
    (peer.repo_root / "parent").symlink_to(tmp_path, target_is_directory=True)
    os.link(tmp_path / "outside.txt", peer.repo_root / "hard")
    os.mkfifo(peer.repo_root / "pipe")
    (peer.repo_root / "binary").write_bytes(b"\x00secret")
    for path in ["escape", "internal", "parent/outside.txt", "hard", "pipe", "binary"]:
        result = peer._read_repo(path)
        assert "error" in result and "HOST_PRIVATE" not in json.dumps(result)


def test_narrow_allowlist_and_read_only_tool_surface(peer):
    peer.allowed_paths = ["src.py"]
    (peer.repo_root / "other.py").write_text("other")
    assert peer._read_repo("src.py")["text"] == "public source\n"
    assert "error" in peer._read_repo("other.py")
    assert peer._read_repo(".")["entries"] == [{"name": "src.py", "type": "file"}]
    assert "error" in peer._tool("shell", '{"command":"id"}')
    assert "error" in peer._tool("read_repo_evidence", '{"path":"src.py","command":"id"}')


def test_stop_timeout_preserves_pending_state_without_permanent_error(peer, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def request(*a, **kw):
        entered.set()
        release.wait(2)
        return Response()
    monkeypatch.setattr(peer._model_http, "post", request)
    peer.start()
    assert entered.wait(1) and peer.pending
    summary = peer.stop(timeout=.01)
    assert summary["inflight_request"] and summary["alive"] and summary["error"] is None
    release.set()
    summary = peer.stop(timeout=1)
    assert peer.finished and not peer.pending
    assert not summary["alive"] and summary["error"] is None


@pytest.mark.parametrize("requested,expected", [(90, 90), (240, 180), (.01, .01), (-1, 0)])
def test_stop_respects_controller_timeout_with_bounded_cap(peer, requested, expected):
    joined = []
    peer._thread = SimpleNamespace(join=lambda *, timeout: joined.append(timeout), is_alive=lambda: True)
    summary = peer.stop(timeout=requested)
    assert joined == [expected]
    assert summary["alive"] and summary["error"] is None


def test_no_send_after_controller_stop(peer):
    peer._stop.set()
    assert "error" in peer._send("must not be delivered")
    assert not peer.channel.sent

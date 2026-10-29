import json
import threading

import pytest

from persistent_peer.channel import SharedChannel
from troy_replication.journal import read_jsonl


@pytest.fixture
def channel(tmp_path, monkeypatch):
    peer = SharedChannel(mattermost_url="http://mattermost.invalid", token="test-secret",
        username="peer-m", channel_id="team-channel", run_dir=tmp_path,
        registry_path=tmp_path / "registry.json", event_path=tmp_path / "events.jsonl",
        allowed_mentions={"A": "worker-a", "B": "worker-b", "C": "worker-c"})
    peer.posts = []
    peer.calls = []
    def request(method, path, **kwargs):
        peer.calls.append((method, path, kwargs))
        if path == "/users/me":
            return {"id": "m", "username": "peer-m"}
        if path == "/channels/team-channel":
            return {"id": "team-channel", "type": "P"}
        if path == "/channels/team-channel/members/m":
            return {"channel_id": "team-channel", "user_id": "m"}
        if path == "/users/a":
            return {"id": "a", "username": "worker-a"}
        if path == "/channels/team-channel/posts":
            page = kwargs["params"]["page"]
            rows = list(reversed(peer.posts))[page * 100:(page + 1) * 100]
            return {"order": [x["id"] for x in rows], "posts": {x["id"]: x for x in rows}}
        if path == "/posts":
            assert json.loads(peer.registry_path.read_text())[-1]["text"] == kwargs["json"]["message"]
            row = {"id": f"p{len(peer.posts)}", "user_id": "m", "create_at": len(peer.posts), **kwargs["json"]}
            peer.posts.append(row)
            return row
        raise AssertionError(path)
    monkeypatch.setattr(peer, "_request", request)
    return peer


def post(number=0, **changes):
    return {"id": f"a{number}", "channel_id": "team-channel", "user_id": "a",
            "message": f"Public update {number}", "create_at": number, **changes}


def test_send_fixed_identity_channel_and_exact_registration(channel):
    result = channel.send_message("Please review this", targets=["A", "C"])
    assert result["text"] == "@worker-a @worker-c\nPlease review this"
    assert result["channel_id"] == "team-channel"
    assert result["visibility"] == "shared_channel_all_members"
    rows = channel.read_messages()
    assert len(rows) == 1 and rows[0]["message_id"] == result["message_id"]
    registry = json.loads(channel.registry_path.read_text())
    assert len(registry) == 1
    assert registry[0]["post_id"] == result["post_id"]
    assert all("direct" not in path for _, path, _ in channel.calls)


@pytest.mark.parametrize("targets", [["D"], ["A", "A"], "A", [1]])
def test_unknown_or_duplicate_targets_rejected_before_transport(channel, targets):
    with pytest.raises(ValueError):
        channel.send_message("text", targets=targets)
    assert channel.calls == []


def test_overlong_message_rejected_including_mentions(channel):
    with pytest.raises(ValueError):
        channel.send_message("x" * 8192, targets=["A"])
    assert channel.calls == []


def test_pagination_registration_and_changed_content_versions(channel):
    channel.posts.extend(post(i) for i in range(105))
    rows = channel.read_messages()
    assert len(rows) == 105 and rows[0]["post_id"] == "a0" and rows[-1]["post_id"] == "a104"
    channel.read_messages()
    assert len(json.loads(channel.registry_path.read_text())) == 105
    channel.posts[0]["message"] = "Edited public update"
    edited = channel.read_messages()
    assert edited[0]["key"] != rows[0]["key"]
    assert len(json.loads(channel.registry_path.read_text())) == 106
    assert len([x for x in read_jsonl(channel.event_path) if x["event"] == "channel_post_observed"]) == 106


def test_shared_channel_member_and_identity_checks_fail_closed(channel, monkeypatch):
    original = channel._request
    monkeypatch.setattr(channel, "_request", lambda method, path, **kw:
        {"id": "team-channel", "type": "D"} if path == "/channels/team-channel" else original(method, path, **kw))
    with pytest.raises(RuntimeError, match="shared_channel"):
        channel.read_messages()


def test_ambiguous_post_is_never_retried(channel, monkeypatch):
    original = channel._request
    count = 0
    def request(method, path, **kw):
        nonlocal count
        if path == "/posts":
            count += 1
            raise TimeoutError("response lost")
        return original(method, path, **kw)
    monkeypatch.setattr(channel, "_request", request)
    with pytest.raises(TimeoutError):
        channel.send_message("one attempt")
    assert count == 1
    rows = json.loads(channel.registry_path.read_text())
    assert len(rows) == 1 and rows[0]["post_id"] is None
    assert read_jsonl(channel.event_path)[-1]["retry_performed"] is False


def test_concurrent_controller_reads_and_peer_sends_register_once(channel):
    channel.posts.append(post())
    failures = []
    def run(action):
        try:
            action()
        except Exception as exc:
            failures.append(exc)
    threads = [threading.Thread(target=run, args=(channel.read_messages,)) for _ in range(8)]
    threads += [threading.Thread(target=run, args=(lambda: channel.send_message("message"),)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert not failures
    channel.read_messages()
    assert len(json.loads(channel.registry_path.read_text())) == 9


def test_cross_channel_response_rejected(channel):
    channel.posts.append(post(channel_id="unexpected"))
    with pytest.raises(RuntimeError, match="cross_channel"):
        channel.read_messages()

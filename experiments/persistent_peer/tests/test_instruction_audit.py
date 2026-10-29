"""The optional instruction audit observes exact wire instructions without changing them."""
import hashlib
import json
from pathlib import Path
import stat

import requests

from peer_influence.usage import read_events
from persistent_peer.tests.test_team_proxy import ACTORS, post, team_relay


def artifacts(live, actor):
    return list((live / "instruction-audit" / actor).glob("*.json"))


def audit_events(live, actor):
    events, errors = read_events(live / f"{actor}.events.jsonl")
    assert not errors
    return [event for event in events if event["event"] == "instruction_audit_saved"]


def test_audit_keeps_the_full_wire_body_identical_and_stores_only_instruction_fields(team_relay):
    before, unaudited, seen_before = team_relay()
    after, audited, seen_after = team_relay(audit=True)
    payload = {"instructions": "SDK original\n保留完整文本", "input": [
        {"role": "user", "content": "DO_NOT_SAVE_TASK_INPUT"},
        {"type": "function_call_output", "call_id": "call1", "output": "DO_NOT_SAVE_TOOL_OUTPUT"},
        {"type": "reasoning", "summary": [{"text": "DO_NOT_SAVE_HIDDEN_REASONING"}]}]}
    assert post(before, "benign_a", **payload).status_code == 200
    assert post(after, "benign_a", **payload).status_code == 200
    assert seen_before[0]["raw_body"] == seen_after[0]["raw_body"]
    assert not (unaudited / "instruction-audit").exists()
    assert not audit_events(unaudited, "benign_a")
    paths = artifacts(audited, "benign_a")
    assert len(paths) == 1
    doc = json.loads(paths[0].read_text())
    effective = seen_after[0]["body"]["instructions"]
    assert doc["effective"]["text"] == effective
    assert doc["original_sdk"]["text"] == payload["instructions"]
    assert doc["pre_policy"]["text"] == payload["instructions"]
    assert doc["injected_policy"]["text"] == (audited / "benign_a.policy").read_text()
    for component in ("effective", "original_sdk", "pre_policy", "injected_policy"):
        assert doc[component]["sha256"] == hashlib.sha256(doc[component]["text"].encode()).hexdigest()
    raw = paths[0].read_bytes()
    for marker in (b"DO_NOT_SAVE_TASK_INPUT", b"DO_NOT_SAVE_TOOL_OUTPUT", b"DO_NOT_SAVE_HIDDEN_REASONING",
                   b"fake-provider-key", b"benign_a-key"):
        assert marker not in raw
    event, = audit_events(audited, "benign_a")
    assert event["effective_sha256"] == hashlib.sha256(effective.encode()).hexdigest()
    assert event["artifact_sha256"] == hashlib.sha256(raw).hexdigest()
    assert event["request_body_sha256"] == hashlib.sha256(seen_after[0]["raw_body"]).hexdigest()
    assert Path(event["artifact_path"]) == paths[0].resolve()
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths[0].parent.stat().st_mode) == 0o700
    usage, _ = read_events(audited / "usage.jsonl")
    assert any(row["event"] == "attempt_started" and row["request_id"] == event["request_id"] for row in usage)


def test_repeated_instructions_deduplicate_sidecars_but_keep_every_request_association(team_relay):
    url, live, seen = team_relay(audit=True)
    assert post(url, "benign_a", input="first task input").status_code == 200
    assert post(url, "benign_a", input="second task input").status_code == 200
    assert len(artifacts(live, "benign_a")) == 1
    events = audit_events(live, "benign_a")
    assert len(events) == 2 and len({event["request_id"] for event in events}) == 2
    assert len({event["artifact_path"] for event in events}) == 1
    assert len({event["request_body_sha256"] for event in events}) == 2
    assert post(url, "benign_a", instructions="changed SDK instructions").status_code == 200
    assert len(artifacts(live, "benign_a")) == 2


def test_authenticated_actor_controls_audit_directory_not_forged_request_identity(team_relay):
    url, live, seen = team_relay(audit=True)
    for actor in ACTORS:
        assert post(url, actor).status_code == 200
        path, = artifacts(live, actor)
        assert json.loads(path.read_text())["agent_id"] == actor
        event, = audit_events(live, actor)
        assert event["agent_id"] == actor
        assert Path(event["artifact_path"]).parent == (live / "instruction-audit" / actor).resolve()
    assert post(url, "forged").status_code == 401
    assert not (live / "instruction-audit" / "forged").exists()


def test_chat_audit_captures_only_system_and_developer_text_without_changing_messages(team_relay):
    before, unaudited, seen_before = team_relay()
    after, audited, seen_after = team_relay(audit=True)
    body = {"messages": [{"role": "system", "content": "SDK chat system"},
                         {"role": "developer", "content": [{"type": "text", "text": "developer text"}]},
                         {"role": "user", "content": "DO_NOT_SAVE_CHAT_TASK"},
                         {"role": "tool", "tool_call_id": "id", "content": "DO_NOT_SAVE_CHAT_TOOL"}]}
    for url in (before, after):
        response = requests.post(url + "/v1/chat/completions", json=body,
                                 headers={"Authorization": "Bearer benign_b-key"}, timeout=5)
        assert response.status_code == 200
    assert seen_before[0]["raw_body"] == seen_after[0]["raw_body"]
    path, = artifacts(audited, "benign_b")
    doc = json.loads(path.read_text())
    effective = json.loads(doc["effective"]["text"])
    assert [message["role"] for message in effective] == ["system", "system", "developer"]
    assert len(json.loads(doc["original_sdk"]["text"])) == 2
    assert "DO_NOT_SAVE_CHAT_TASK" not in path.read_text()
    assert "DO_NOT_SAVE_CHAT_TOOL" not in path.read_text()


def test_no_instruction_field_is_preserved_as_absent_before_policy_injection(team_relay):
    url, live, _ = team_relay(audit=True)
    assert requests.post(url + "/v1/responses", json={"input": "task"},
                         headers={"Authorization": "Bearer benign_c-key"}, timeout=5).status_code == 200
    path, = artifacts(live, "benign_c")
    doc = json.loads(path.read_text())
    assert doc["original_sdk"]["field_present"] is False
    assert doc["original_sdk"]["text"] is None and doc["original_sdk"]["sha256"] is None
    assert doc["effective"]["text"] == doc["injected_policy"]["text"]

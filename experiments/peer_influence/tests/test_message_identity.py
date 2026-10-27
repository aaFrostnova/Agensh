import hashlib
import json

import pytest

from peer_influence.controller.app import ControllerConfig, TreatmentController
from peer_influence.controller.mattermost import MattermostClient
from peer_influence.tests.fake_mattermost import FakeMattermost

FAILED = {"event_type": "check_result", "exit_code": 1}


@pytest.fixture()
def mm():
    server = FakeMattermost()
    yield server
    server.close()


def run_with(tmp_path, mm, peer_token):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(FAILED) + "\n")
    cfg = ControllerConfig(
        experiment_id="exp", episode_id="ep_000009", run_dir=tmp_path / "run", treatment_type="malicious",
        message="peer says hello", mattermost_url=mm.url, peer_token=peer_token,
        peer_username="org-worker2", victim_username="org-worker1", events_path=events,
    )
    cfg.run_dir.mkdir()
    c = TreatmentController(cfg, mm=MattermostClient(mm.url, peer_token), clock=lambda: 50.0)
    c.step()
    return json.loads((cfg.run_dir / "controller_state.json").read_text()), cfg


def test_message_is_authored_by_the_peer_account_in_a_dm_with_the_victim(tmp_path, mm):
    state, _ = run_with(tmp_path, mm, "tok_w2")
    (post,) = mm.posts
    assert post["user_id"] == "u_w2"  # worker2, not the admin
    assert mm.channels[post["channel_id"]]["members"] == ["u_w1", "u_w2"]
    assert post["props"]["idempotency_key"] == "exp:ep_000009:malicious"
    assert state["sent"]["author_username"] == "org-worker2" and state["sent"]["author_id"] == "u_w2"


def test_sending_with_the_admin_token_fails_closed(tmp_path, mm):
    state, _ = run_with(tmp_path, mm, "tok_admin")
    assert state["state"] == "FAILED_IDENTITY"
    assert mm.posts == []


def test_unknown_token_never_posts(tmp_path, mm):
    state, _ = run_with(tmp_path, mm, "tok_nobody")
    assert mm.posts == [] and state["state"] in ("SENDING", "FAILED_SEND")


def test_message_log_records_author_channel_and_hash(tmp_path, mm):
    _, cfg = run_with(tmp_path, mm, "tok_w2")
    (line,) = (cfg.run_dir / "messages.jsonl").read_text().splitlines()
    rec = json.loads(line)
    assert rec["author_username"] == "org-worker2" and rec["recipient_username"] == "org-worker1"
    assert rec["message_sha256"] == hashlib.sha256(b"peer says hello").hexdigest()
    assert rec["post_id"] == mm.posts[0]["id"] and rec["channel_id"] == mm.posts[0]["channel_id"]
    assert rec["idempotency_key"] == "exp:ep_000009:malicious"


def test_message_log_is_not_duplicated_by_a_restart(tmp_path, mm):
    _, cfg = run_with(tmp_path, mm, "tok_w2")
    c = TreatmentController(cfg, mm=MattermostClient(mm.url, "tok_w2"), clock=lambda: 60.0)
    c.step()
    assert len((cfg.run_dir / "messages.jsonl").read_text().splitlines()) == 1


def test_treatment_without_a_message_is_a_configuration_error(tmp_path, mm):
    with pytest.raises(ValueError, match="needs a message"):
        ControllerConfig(
            experiment_id="e", episode_id="x", run_dir=tmp_path, treatment_type="benign", message=None,
            mattermost_url=mm.url, peer_token="tok_w2", peer_username="org-worker2",
            victim_username="org-worker1", events_path=tmp_path / "e.jsonl",
        )

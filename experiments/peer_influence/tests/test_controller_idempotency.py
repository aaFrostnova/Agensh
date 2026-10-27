import json

import pytest

from peer_influence.controller.app import ControllerConfig, TreatmentController
from peer_influence.controller.mattermost import MattermostClient
from peer_influence.tests.fake_mattermost import FakeMattermost

MESSAGE = "benign-looking test message"
FAILED = {"event_type": "check_result", "exit_code": 1, "passed": 1, "failed": 2}
PASSED = {"event_type": "check_result", "exit_code": 0, "passed": 3, "failed": 0}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture()
def mm():
    server = FakeMattermost()
    yield server
    server.close()


def make(tmp_path, mm, clock=None, treatment="benign", **over):
    cfg = dict(
        experiment_id="exp", episode_id="ep_000001", run_dir=tmp_path / "run", treatment_type=treatment,
        message=None if treatment == "none" else MESSAGE, mattermost_url=mm.url, peer_token="tok_w2",
        peer_username="org-worker2", victim_username="org-worker1",
        events_path=tmp_path / "events" / "events.jsonl", fallback_after_seconds=180.0,
    )
    cfg.update(over)
    config = ControllerConfig(**cfg)
    config.run_dir.mkdir(parents=True, exist_ok=True)
    return TreatmentController(config, mm=MattermostClient(mm.url, config.peer_token), clock=clock or Clock())


def emit(tmp_path, *events, raw=None):
    path = tmp_path / "events" / "events.jsonl"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
        if raw:
            f.write(raw)


def state(tmp_path):
    return json.loads((tmp_path / "run" / "controller_state.json").read_text())


def test_nothing_is_sent_until_the_first_failed_check(tmp_path, mm):
    c = make(tmp_path, mm)
    assert c.step() == "WAITING_FOR_FIRST_FAILED_CHECK"
    emit(tmp_path, PASSED)
    assert c.step() == "WAITING_FOR_FIRST_FAILED_CHECK" and mm.posts == []
    emit(tmp_path, FAILED)
    assert c.step() == "TREATMENT_SENT"
    assert len(mm.posts) == 1 and mm.posts[0]["message"] == MESSAGE
    assert state(tmp_path)["trigger_source"] == "first_failed_check"


def test_many_failed_checks_still_send_exactly_once(tmp_path, mm):
    c = make(tmp_path, mm)
    emit(tmp_path, FAILED, FAILED, FAILED)
    for _ in range(5):
        c.step()
    emit(tmp_path, FAILED)
    for _ in range(5):
        c.step()
    assert len(mm.posts) == 1


def test_restart_after_sent_never_resends(tmp_path, mm):
    emit(tmp_path, FAILED)
    first = make(tmp_path, mm)
    first.step()
    post_id = state(tmp_path)["sent"]["post_id"]
    emit(tmp_path, FAILED)
    second = make(tmp_path, mm)  # a fresh process on the same run directory
    assert second.step() == "TREATMENT_SENT"
    assert len(mm.posts) == 1 and state(tmp_path)["sent"]["post_id"] == post_id


def test_lost_response_is_recovered_by_adopting_the_existing_post(tmp_path, mm):
    mm.fail_after_create_next = 1  # server stores the post, the client only sees HTTP 500
    emit(tmp_path, FAILED)
    first = make(tmp_path, mm)
    assert first.step() == "SENDING"
    assert len(mm.posts) == 1
    second = make(tmp_path, mm)  # restart while the durable state says SENDING
    assert second.step() == "TREATMENT_SENT"
    assert len(mm.posts) == 1
    assert state(tmp_path)["sent"]["post_id"] == mm.posts[0]["id"]


def test_transient_failure_is_retried_and_produces_one_post(tmp_path, mm):
    mm.fail_next_posts = 2
    emit(tmp_path, FAILED)
    c = make(tmp_path, mm)
    states = [c.step() for _ in range(4)]
    assert states[-1] == "TREATMENT_SENT" and len(mm.posts) == 1
    assert state(tmp_path)["send_attempts"] == 2


def test_gives_up_after_max_attempts_without_sending(tmp_path, mm):
    mm.fail_next_posts = 99
    emit(tmp_path, FAILED)
    c = make(tmp_path, mm, max_send_attempts=3)
    for _ in range(6):
        c.step()
    assert state(tmp_path)["state"] == "FAILED_SEND" and mm.posts == []


def test_fallback_timer_triggers_when_the_agent_never_runs_check(tmp_path, mm):
    clock = Clock()
    c = make(tmp_path, mm, clock=clock)
    c.step()
    clock.now += 179
    assert c.step() == "WAITING_FOR_FIRST_FAILED_CHECK"
    clock.now += 2
    assert c.step() == "TREATMENT_SENT"
    s = state(tmp_path)
    assert s["trigger_source"] == "fallback_timer" and s["sent"]["sent_at"] == clock.now


def test_solo_episode_records_the_trigger_but_sends_nothing(tmp_path, mm):
    c = make(tmp_path, mm, treatment="none")
    emit(tmp_path, FAILED)
    assert c.step() == "TRIGGERED_NO_TREATMENT"
    assert mm.posts == [] and state(tmp_path)["trigger_source"] == "first_failed_check"
    assert state(tmp_path)["sent"] is None


def test_partial_and_malformed_event_lines_are_not_misread(tmp_path, mm):
    c = make(tmp_path, mm)
    emit(tmp_path, raw='{"event_type": "check_res')  # writer is mid-line
    assert c.step() == "WAITING_FOR_FIRST_FAILED_CHECK"
    emit(tmp_path, raw='ult", "exit_code": 1}\nnot json at all\n')
    assert c.step() == "TREATMENT_SENT"
    assert state(tmp_path)["malformed_events"] == 1


def test_finish_marks_the_episode_done_and_keeps_the_record(tmp_path, mm):
    c = make(tmp_path, mm)
    emit(tmp_path, FAILED)
    c.step()
    c.finish()
    s = state(tmp_path)
    assert s["state"] == "EPISODE_DONE" and s["sent"]["post_id"] and s["key"] == "exp:ep_000001:benign"
    assert make(tmp_path, mm).step() == "EPISODE_DONE" and len(mm.posts) == 1


def ack(tmp_path, event_id):
    return json.loads((tmp_path / "events" / "acks" / f"{event_id}.json").read_text())


def test_every_check_event_is_acknowledged_and_the_trigger_ack_follows_the_send(tmp_path, mm):
    c = make(tmp_path, mm)
    emit(tmp_path, {**PASSED, "event_id": "e-pass"}, {**FAILED, "event_id": "e-fail1"}, {**FAILED, "event_id": "e-fail2"})
    c.step()
    assert ack(tmp_path, "e-pass") == {"will_send": False}
    first = ack(tmp_path, "e-fail1")
    assert first["will_send"] is True and first["post_id"] == mm.posts[0]["id"]
    assert ack(tmp_path, "e-fail2") == {"will_send": False}


def test_trigger_ack_waits_while_the_send_is_still_being_retried(tmp_path, mm):
    mm.fail_next_posts = 1
    c = make(tmp_path, mm)
    emit(tmp_path, {**FAILED, "event_id": "e-fail"})
    assert c.step() == "SENDING"
    assert not (tmp_path / "events" / "acks" / "e-fail.json").exists()  # the DM is not out yet
    assert c.step() == "TREATMENT_SENT"
    assert ack(tmp_path, "e-fail")["will_send"] is True


def test_solo_never_promises_a_message(tmp_path, mm):
    c = make(tmp_path, mm, treatment="none")
    emit(tmp_path, {**FAILED, "event_id": "e-solo"})
    c.step()
    assert ack(tmp_path, "e-solo") == {"will_send": False}


def test_event_ids_from_the_agent_writable_log_cannot_escape_the_ack_directory(tmp_path, mm):
    c = make(tmp_path, mm)
    emit(tmp_path, {**FAILED, "event_id": "../../escape"}, {**FAILED, "event_id": "fine-id"})
    c.step()
    assert not list(tmp_path.glob("escape*")) and not (tmp_path / "escape.json").exists()
    assert not list((tmp_path / "events").glob("*.json"))
    assert ack(tmp_path, "fine-id") == {"will_send": False}  # not the trigger: the traversal id was first

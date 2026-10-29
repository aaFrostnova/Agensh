from troy_replication.journal import emit
from troy_replication.lifecycle import LifecycleTracker
from troy_replication.runner import external_verdict, message_evidence, replay


def test_external_collection_and_infrastructure_failures_are_not_zeroes():
    for code in (2, 3, 4, 5, -9):
        assert external_verdict({'returncode': code, 'substantive_verdict': True,
                                 'timed_out': False, 'passed': False}) is None
    assert external_verdict({'returncode': 1, 'substantive_verdict': False}) is None
    assert external_verdict({'returncode': 0, 'substantive_verdict': True,
                             'timed_out': True, 'passed': True}) is None
    assert external_verdict({'returncode': 1, 'substantive_verdict': True,
                             'passed': False}) is False
    assert external_verdict({'returncode': 0, 'substantive_verdict': True,
                             'passed': True}) is True


def test_social_message_counts_do_not_include_task_kickoff():
    events = [
        {'event':'message_sent', 'event_id':'e1', 'message_id':'kickoff', 'post_id':'p1', 'social':False},
        {'event':'message_sent', 'event_id':'e2', 'message_id':'peer1', 'post_id':'p2', 'social':True},
        {'event':'acp_prompt_started', 'event_id':'e3', 'turn_id':'t1',
         'source_event_ids':['mattermost:post:p1:worker', 'mattermost:post:p2:worker']},
        {'event':'message_context_included', 'event_id':'e4', 'message_id':'peer1', 'post_id':'p2'},
        {'event':'reply_received_by_adversary', 'event_id':'e5', 'message_id':'peer1'},
    ]
    tracker = LifecycleTracker()
    for event in events:
        tracker.observe(event)
    normalized = message_evidence(tracker, events, social_only=True)
    assert {e['message_id'] for e in normalized} == {'peer1'}
    assert [e['event'] for e in normalized] == ['sent','delivered','included_in_context','reply_received_by_adversary']
    assert [e['event_id'] for e in normalized] == ['e2','e3','e4','e5']


def test_replay_orders_iso_and_epoch_timestamps(tmp_path):
    import json
    events = [
        {'event':'acp_prompt_finished', 'event_id':'done', 'turn_id':'t', 'stop_reason':'end_turn',
         'timestamp':'2026-10-29T00:00:01Z'},
        {'event':'acp_prompt_started', 'event_id':'start', 'turn_id':'t', 't':1793232000.0},
    ]
    path = tmp_path/'events.jsonl'
    path.write_text(''.join(json.dumps(event)+'\n' for event in events))
    tracker = replay(path)
    assert not tracker.active_prompts
    assert tracker.last_stop_reason == 'end_turn'

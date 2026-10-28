import json

from peer_influence.usage import aggregate_usage, append_event, extract_usage, main, summarize_episode_usage


def usage(**kw):
    return {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 15},
            "input_tokens_details": {"cached_tokens": 30}, **kw}


def sse(*events):
    return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()


def complete_events(agent="victim", request="r1", episode="ep1", **kw):
    common = dict(agent_id=agent, request_id=request, episode_id=episode, cohort_id="cohort")
    u = extract_usage(json.dumps({"usage": usage(**kw)}))
    return [{**common, "event": "request_started"}, {**common, "event": "attempt_started", "attempt": 1},
            {**common, "event": "attempt_finished", "attempt": 1, "usage": u},
            {**common, "event": "request_finished"}]


def test_json_reasoning_and_cache_are_subsets_never_added_to_total():
    result = extract_usage(json.dumps({"usage": usage()}))
    assert result["total_tokens"] == 100 and result["reasoning_tokens"] == 15
    assert result["cached_input_tokens"] == 30 and result["accounting_complete"]


def test_chat_completion_usage_and_derived_total():
    result = extract_usage(json.dumps({"usage": {"prompt_tokens": 3, "completion_tokens": 4,
                       "completion_tokens_details": {"reasoning_tokens": 2}}}))
    assert result["total_tokens"] == 7 and result["reasoning_tokens"] == 2
    assert result["cached_input_tokens"] is None


def test_sse_uses_final_cumulative_snapshot_once():
    result = extract_usage(sse({"type": "response.created", "response": {"usage": usage(output_tokens=1)}},
                              {"type": "response.completed", "response": {"id": "resp1", "usage": usage()}}),
                           "text/event-stream")
    assert result["total_tokens"] == 100 and result["response_id"] == "resp1"
    assert result["accounting_complete"]


def test_chat_sse_done_terminal_and_crlf_frames():
    raw = (sse({"usage": usage()}) + b'data: [DONE]\n\n').replace(b'\n', b'\r\n')
    assert extract_usage(raw, "text/event-stream")["accounting_complete"]


def test_responses_terminal_without_usage_does_not_promote_early_snapshot_to_total():
    raw = sse({"type": "response.created", "response": {
                  "usage": {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10}}},
              {"type": "response.completed", "response": {"id": "r", "status": "completed"}})
    for data in (raw, raw + b'data: [DONE]\n\n'):
        result = extract_usage(data, "text/event-stream")
        assert result["terminal_observed"]
        assert result["total_tokens"] == 10  # Known lower bound, never an exact total.
        assert not result["accounting_complete"] and result["usage_status"] == "partial"
        events = complete_events()
        events[2]["usage"] = result
        summary = aggregate_usage(events)["total"]
        assert summary["totals"]["total_tokens"] is None
        assert summary["known_tokens"]["total_tokens"] == 10


def test_sse_missing_terminal_or_truncated_transport_is_incomplete():
    for raw, transport in [(sse({"usage": usage()}), True),
                           (sse({"type": "response.completed", "response": {"usage": usage()}}), False)]:
        result = extract_usage(raw, "text/event-stream", transport)
        assert not result["accounting_complete"] and result["usage_status"] == "partial"


def test_absent_usage_stays_null():
    for raw in (b'{}', b'{"usage": null}', b'data: [DONE]\n\n', b'broken'):
        result = extract_usage(raw)
        assert result["total_tokens"] is None and not result["accounting_complete"]


def test_aggregate_two_agents_episode_cohort_and_dedupe():
    events = complete_events() + complete_events("adversary", "r2", "ep2")
    result = aggregate_usage(events + events)
    assert result["total"]["totals"]["total_tokens"] == 200
    assert result["agents"]["victim"]["totals"]["total_tokens"] == 100
    assert result["episodes"]["ep2"]["totals"]["total_tokens"] == 100
    assert result["cohorts"]["cohort"]["totals"]["total_tokens"] == 200
    assert result["cohorts"]["cohort"]["agents"]["adversary"]["totals"]["total_tokens"] == 100
    assert result["episodes"]["ep1"]["agents"]["victim"]["totals"]["total_tokens"] == 100


def test_interrupted_attempt_preserves_known_lower_bound_and_unknown_total():
    events = complete_events() + complete_events(request="r2")[:2]
    result = aggregate_usage(events)["total"]
    assert result["unfinished_requests"] == 1 and result["unfinished_attempts"] == 1
    assert result["totals"]["total_tokens"] is None
    assert result["known_tokens"]["total_tokens"] == 100
    assert not result["accounting_complete"]


def test_missing_ledger_is_not_a_zero_token_episode(tmp_path):
    result = summarize_episode_usage(tmp_path / 'missing', ['victim'])
    assert result["agents"]["victim"]["totals"]["total_tokens"] is None
    assert result["total"]["ledger_errors"] == ["ledger_missing"]


def test_empty_valid_ledger_is_exact_zero_for_expected_agents(tmp_path):
    path = tmp_path / 'usage.jsonl'
    path.touch()
    result = summarize_episode_usage(path, ['victim', 'adversary'])
    assert result["agents"]["adversary"]["totals"]["total_tokens"] == 0


def test_damaged_ledger_retains_known_counts_but_invalidates_completeness(tmp_path):
    path = tmp_path / 'usage.jsonl'
    for e in complete_events(): append_event(path, e)
    with path.open('a') as out: out.write('{"cutoff":')
    result = summarize_episode_usage(path)["total"]
    assert result["known_tokens"]["total_tokens"] == 100
    assert result["totals"]["total_tokens"] is None


def test_missing_reasoning_detail_is_not_zero_even_with_complete_primary_usage():
    result = extract_usage(json.dumps({"usage": {"input_tokens": 5, "output_tokens": 3}}))
    assert result["accounting_complete"] and result["reasoning_tokens"] is None


def test_cli_discovers_runner_ledger_name_in_nested_cohorts_and_deduplicates(tmp_path, capsys):
    ledger = tmp_path / 'cohort' / 'episodes' / 'ep1' / 'usage_events.jsonl'
    ledger.parent.mkdir(parents=True)
    for event in complete_events(): append_event(ledger, event)
    assert main([str(tmp_path), str(ledger)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['total']['requests'] == 1
    assert result['cohorts']['cohort']['agents']['victim']['totals']['total_tokens'] == 100

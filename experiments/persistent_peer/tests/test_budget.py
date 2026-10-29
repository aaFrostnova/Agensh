"""Shared admission accounting uses only mock usage; no paid API requests."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing

import pytest

from persistent_peer.budget import TeamBudget, unbounded_input_reference


def usage(inp, out, **extra):
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
            "accounting_complete": True, "cached_input_tokens": inp // 2,
            "reasoning_tokens": out // 2, **extra}


def budget(tmp_path, **kwargs):
    return TeamBudget(tmp_path / "budget.json", **kwargs)


def test_three_actors_share_provider_input_and_output_but_adversary_is_unlimited(tmp_path):
    ledger = budget(tmp_path, limit=150_000)
    for index, actor in enumerate(("benign_a", "benign_b", "benign_c")):
        assert ledger.reserve(actor, str(index), 1)["allowed"]
        ledger.settle(actor, str(index), 1, usage(20_000, 1000))
    before = ledger.snapshot()
    for index in range(100):
        assert ledger.reserve("adversary", f"adv{index}", 1)["allowed"]
        ledger.settle("adversary", f"adv{index}", 1, usage(1_000_000, 8192))
    state = ledger.snapshot()
    assert state["known_input_output_tokens"] == state["committed_tokens"] == 63_000
    assert state["entries"] == before["entries"]
    assert state["remaining_admission_tokens"] == 87_000


def test_missing_retry_usage_retains_reservation_instead_of_counting_as_zero(tmp_path):
    ledger = budget(tmp_path, limit=150_000)
    assert ledger.reserve("benign_a", "request", 1)["reservation_tokens"] == 73728
    ledger.settle("benign_a", "request", 1, {"accounting_complete": False})
    assert ledger.reserve("benign_a", "request", 2)["allowed"]
    ledger.settle("benign_a", "request", 2, usage(100, 20))
    state = ledger.snapshot()
    assert state["committed_tokens"] == 73728 + 120
    assert state["unknown_reserved_tokens"] == 73728
    assert state["known_input_output_tokens"] == 120
    assert state["unknown_usage_attempts"] == 1


def test_partial_usage_keeps_entire_reservation(tmp_path):
    ledger = budget(tmp_path)
    ledger.reserve("benign_a", "x", 1)
    ledger.settle("benign_a", "x", 1, usage(100, 20, accounting_complete=False))
    state = ledger.snapshot()
    assert state["known_input_output_tokens"] == 120
    assert state["committed_tokens"] == 73728


def test_dynamic_reservation_uses_the_final_serialized_request_size(tmp_path):
    ledger = budget(tmp_path)
    result = ledger.reserve("benign_a", "large", 1, request_bytes=100000)
    assert result["reservation_tokens"] == 100000 + 16384 + 8192
    assert "serialized_request_utf8_bytes" in result["reservation_method"]


def test_exhaustion_reports_unused_tail_and_is_sticky_for_all_benign_actors(tmp_path):
    ledger = budget(tmp_path, limit=80000)
    assert ledger.reserve("benign_a", "first", 1)["allowed"]
    rejected = ledger.reserve("benign_b", "second", 1)
    assert rejected["allowed"] is False
    assert rejected["reason"] == "reservation_exhausted"
    assert rejected["remaining_admission_tokens"] == 6272
    ledger.settle("benign_a", "first", 1, usage(100, 20))
    assert ledger.reserve("benign_c", "third", 1)["allowed"] is False
    assert ledger.snapshot()["known_input_output_tokens"] == 120


def test_inflight_reservation_survives_reopening_and_cannot_be_reset(tmp_path):
    ledger = budget(tmp_path)
    ledger.reserve("benign_a", "crashed", 1)
    reopened = budget(tmp_path)
    assert reopened.snapshot()["inflight_reserved_tokens"] == 73728
    with pytest.raises(ValueError, match="configuration mismatch"):
        budget(tmp_path, limit=3_000_000).snapshot()
    with pytest.raises(ValueError, match="already reserved"):
        reopened.reserve("benign_a", "crashed", 1)
    with pytest.raises(ValueError, match="mismatched"):
        reopened.settle("benign_b", "crashed", 1, usage(1, 1))


def test_provider_exceeding_reservation_is_visible_and_stops_admission(tmp_path):
    ledger = budget(tmp_path)
    ledger.reserve("benign_a", "unexpected", 1)
    result = ledger.settle("benign_a", "unexpected", 1, usage(80_000, 10))
    assert result["exhausted"] and result["stop_reason"] == "budget_integrity_error"
    state = ledger.snapshot()
    assert state["known_input_output_tokens"] == 80010
    assert state["committed_tokens"] == 80010
    assert state["integrity_error"]["reason"] == "provider_usage_exceeded_reservation"
    assert not ledger.reserve("benign_b", "next", 1)["allowed"]


def test_parallel_threads_cannot_overreserve_shared_balance(tmp_path):
    ledger = budget(tmp_path, limit=73728 * 3)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda index: ledger.reserve(("benign_a", "benign_b", "benign_c")[index % 3],
                                                            str(index), 1), range(12)))
    assert sum(result["allowed"] for result in results) == 3
    state = ledger.snapshot()
    assert state["committed_tokens"] == state["limit_tokens"]
    assert state["inflight_attempts"] == 3
    assert json.loads((tmp_path / "budget.json").read_text())["exhausted"]


def _process_reserve(args):
    path, index = args
    return TeamBudget(path, limit=73728 * 2).reserve("benign_a", str(index), 1)["allowed"]


def test_separate_processes_share_the_same_lock(tmp_path):
    path = str(tmp_path / "budget.json")
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        results = pool.map(_process_reserve, [(path, index) for index in range(8)])
    assert sum(results) == 2
    assert TeamBudget(path, limit=73728 * 2).snapshot()["committed_tokens"] == 73728 * 2


@pytest.mark.parametrize("payload", [
    {"previous_response_id": "secret-context"}, {"conversation": "stateful"},
    {"input": [{"type": "input_file", "file_id": "file-x"}]},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "https://example.test/x"}]}]},
    {"tools": [{"type": "file_search"}]}, {"prompt": {"id": "saved-prompt"}},
    {"tools": [{"type": "mcp", "server_url": "https://example.test"}]}, {"input": [{"id": "stored-message"}]},
])
def test_unbounded_provider_references_are_rejected(payload):
    assert unbounded_input_reference(payload)


def test_complete_text_and_function_tool_history_is_supported():
    assert unbounded_input_reference({"input": [{"role": "user", "content": "Read file_id in this literal string"},
        {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": '{"file_id":"local"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "literal content"}],
        "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}) is None


def test_json_schema_property_names_and_union_types_are_not_input_references():
    assert unbounded_input_reference({"input": "hello", "tools": [{"type": "function", "name": "inspect",
        "parameters": {"type": "object", "properties": {"file_id": {"type": ["string", "null"]}}}}],
        "text": {"format": {"type": "json_schema", "schema": {"type": "object",
            "properties": {"image_url": {"type": ["string", "null"]}}}}}}) is None

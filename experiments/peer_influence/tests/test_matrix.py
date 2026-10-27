import json
from collections import Counter

import pytest

from peer_influence.manifest import (
    MissingTreatmentMessage,
    build_manifest,
    load_treatment_message,
    prompt_hashes,
)
from peer_influence.matrix import CONDITIONS, plan_matrix
from peer_influence.routes import load_routes, route_hash
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[1] / "configs" / "model_routes.yaml"
PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def test_every_cell_appears_exactly_repeats_times_per_task_pair():
    plan = plan_matrix(task_pairs=2, repeats=2, route="r", experiment_id="e", seed=1)
    assert len(plan) == 24
    cells = Counter((s["pair_id"], s["difficulty"], s["peer_condition"]) for s in plan)
    assert len(cells) == 12 and set(cells.values()) == {2}
    assert {s["seed"] for s in plan} == {1, 2}
    assert set(CONDITIONS) == {(d, p) for d in ("solvable", "impossible") for p in ("solo", "benign", "malicious")}


def test_smoke_and_pilot_sizes_match_the_plan():
    assert len(plan_matrix(2, 2, "r", "e", 1)) == 24
    assert len(plan_matrix(10, 3, "r", "e", 1)) == 180


def test_order_is_reproducible_shuffled_and_independent_per_route():
    a = plan_matrix(3, 2, "qwen3_8_27b_local", "e", 7)
    assert a == plan_matrix(3, 2, "qwen3_8_27b_local", "e", 7)
    order = [(s["pair_id"], s["difficulty"], s["peer_condition"], s["seed"]) for s in a]
    assert order != sorted(order)
    b = plan_matrix(3, 2, "kimi_k3_bedrock", "e", 7)
    assert [(s["pair_id"], s["difficulty"], s["peer_condition"], s["seed"]) for s in b] != order


def test_episode_ids_follow_run_order_and_task_ids_name_the_variant():
    plan = plan_matrix(2, 1, "r", "e", 3)
    assert [s["episode_id"] for s in plan] == [f"ep_{i:06d}" for i in range(1, 13)]
    assert all(s["task_id"] == f"{s['pair_id']}_{s['difficulty']}" and s["run_attempt"] == 1 for s in plan)


def test_missing_treatment_message_fails_closed(tmp_path):
    (tmp_path / "peer_benign.txt").write_text("hello\n")
    assert load_treatment_message(tmp_path, "solo") == (None, None)
    text, sha = load_treatment_message(tmp_path, "benign")
    assert text == "hello" and len(sha) == 64
    with pytest.raises(MissingTreatmentMessage, match="peer_malicious.txt"):
        load_treatment_message(tmp_path, "malicious")
    (tmp_path / "peer_malicious.txt").write_text("  \n")
    with pytest.raises(MissingTreatmentMessage):
        load_treatment_message(tmp_path, "malicious")


def test_prompt_hashes_cover_victim_brief_and_each_present_peer_message(tmp_path):
    for name in ("victim.md", "task_brief.md", "peer_benign.txt"):
        (tmp_path / name).write_text(name)
    h = prompt_hashes(tmp_path)
    assert set(h) == {"victim_prompt_sha256", "task_brief_sha256", "peer_benign_sha256"}
    (tmp_path / "peer_malicious.txt").write_text("x")
    assert "peer_malicious_sha256" in prompt_hashes(tmp_path)


def test_manifest_carries_the_plan_fields_and_the_route_identity():
    route = load_routes(ROUTES)["qwen3_8_27b_local"]
    spec = plan_matrix(1, 1, route.name, "peer_influence_pilot_v0.2.0", 5)[0]
    m = build_manifest(
        spec, route=route, task_manifest={"pair_id": spec["pair_id"], "task_id": spec["task_id"], "bundle_sha256": "b"},
        prompt_hashes={"victim_prompt_sha256": "v"}, code_commit="abc", agensh_commit="0468a3e",
        peer_message_sha256="m", serving={"vllm_version": "0.26.0"},
    )
    for key in ("experiment_id", "episode_id", "agensh_commit", "experiment_code_commit", "model_route", "model_id",
                "provider", "reasoning_effort", "max_prompt_tokens", "task_id", "task_pair_id", "difficulty",
                "peer_condition", "peer_message_template_hash", "victim_prompt_hash", "seed", "run_attempt"):
        assert key in m, key
    assert (m["model_id"], m["reasoning_effort"], m["max_prompt_tokens"]) == ("Qwen/Qwen3.8-27B", "medium", 65536)
    assert m["route_hash"] == route_hash(route) and m["serving"] == {"vllm_version": "0.26.0"}
    assert json.dumps(m)  # serializable

import json
import shutil

import pytest

from peer_influence.common import sha256_bytes, tree_sha256, write_json_atomic
from peer_influence.evaluation.quality import CHECKS, quality_report
from peer_influence.routes import load_routes, route_hash
from peer_influence.tasks.generate import load_manifest
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[1] / "configs" / "model_routes.yaml"
ROUTE = load_routes(ROUTES)["qwen3_8_27b_local"]
PROMPT = "victim prompt text\n"
PEER = "org-worker2"


def wj(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def build_good_run(tmp_path, task_dir, peer_condition="malicious"):
    """A run directory shaped like the runner's output (the evidence contract)."""
    run = tmp_path / "run"
    snap = run / "workspace_snapshots" / "final"
    shutil.copytree(task_dir / "repo", snap)
    (snap / "AGENTS.md").write_text(PROMPT)
    wj(snap / ".mcp.json", {"mcpServers": {"gitea": {}, "mattermost": {}}})
    tm = load_manifest(task_dir)
    wj(run / "task_manifest.json", tm)
    wj(run / "manifest.json", {
        "episode_id": "ep_000001", "model_route": ROUTE.name, "route_hash": route_hash(ROUTE), "model_id": ROUTE.model_id,
        "peer_condition": peer_condition, "peer_username": PEER, "fresh_profile": True,
        "victim_started_at": 100.0, "victim_prompt_hash": sha256_bytes(PROMPT.encode()),
        "workspace_seed_sha256": tm["bundle_sha256"],
    })
    wj(run / "prompt_hashes.json", {"victim_prompt_sha256": sha256_bytes(PROMPT.encode())})
    wj(run / "launch_evidence.json", {"agents_md_sha256": sha256_bytes(PROMPT.encode()), "mcp_servers": ["gitea", "mattermost"]})
    wj(run / "worker_env.json", {
        "ANTHROPIC_MODEL": ROUTE.model_id, "COPILOT_AGENT_REASONING_EFFORT": "medium",
        "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": "65536", "COPILOT_PROVIDER_WIRE_API": "responses",
        "EXPERIMENT_DISABLE_BOARD": "1",
    })
    (run / "logs").mkdir()
    (run / "logs" / "worker.log").write_text(f"[entrypoint] omcgitea ... provider=http://x model={ROUTE.model_id}\n")
    wj(run / "relay_summary.json", {"responses_ok": 12, "responses_error": 0})
    wj(run / "preflight.json", {"ok": True, "route_hash": route_hash(ROUTE), "checks": {
        "responses_text": {"ok": True}, "responses_tool_call": {"ok": True}, "tool_result_roundtrip": {"ok": True}}})
    wj(run / "trajectory_summary.json", {"archive_manifest_complete": True, "worker_exit_code": 0})
    wj(run / "snapshot_summary.json", {"live_tree_sha256": tree_sha256(snap), "copy_tree_sha256": tree_sha256(snap)})
    wj(run / "protected_hashes_after.json", {"patterns": [], "hashes": {}})
    wj(run / "score_recheck.json", {"deterministic": True})
    traj = run / "agensh_trajectory"
    traj.mkdir()
    (traj / "session.log").write_text("x")
    if peer_condition == "solo":
        (run / "messages.jsonl").write_text("")
        wj(run / "dm_posts.json", {"posts": [{"user": "org-worker0", "id": "p0", "props": {}}]})
        wj(run / "router_events.json", [])
        wj(run / "shown_events.json", [])
        wj(run / "controller_state.json", {"state": "EPISODE_DONE", "sent": None})
    else:
        (run / "messages.jsonl").write_text(json.dumps({"post_id": "p1", "author_username": PEER}) + "\n")
        wj(run / "dm_posts.json", {"posts": [{"user": PEER, "id": "p1", "props": {"idempotency_key": "e:ep_000001:" + peer_condition}}]})
        wj(run / "router_events.json", [{"id": "7", "dedup_key": "mattermost:post:p1:0", "state": "sent", "agent": "org-worker1"}])
        wj(run / "shown_events.json", [])
        wj(run / "controller_state.json", {"state": "EPISODE_DONE", "sent": {"post_id": "p1", "author_username": PEER}})
    return run


def report(run, task_dir):
    return quality_report(run, routes_file=ROUTES, task_dir=task_dir)


def test_a_complete_run_is_valid_and_every_check_is_true(tmp_path, impossible_dir):
    r = report(build_good_run(tmp_path, impossible_dir), impossible_dir)
    assert r["valid"] is True and r["invalid_reasons"] == []
    assert set(r["checks"]) == set(CHECKS) and all(r["checks"].values())


def test_a_complete_solo_run_is_valid(tmp_path, impossible_dir):
    r = report(build_good_run(tmp_path, impossible_dir, "solo"), impossible_dir)
    assert r["valid"] is True, r


def damage_file(name):
    return lambda run: (run / name).unlink()


def edit_json(name, fn):
    def apply(run):
        obj = json.loads((run / name).read_text())
        fn(obj)
        (run / name).write_text(json.dumps(obj))
    return apply


DAMAGE = {
    "fresh_profile": edit_json("manifest.json", lambda m: m.update(fresh_profile=False)),
    "victim_started": edit_json("relay_summary.json", lambda m: m.update(responses_ok=0)),
    "model_route_resolved": edit_json("manifest.json", lambda m: m.update(route_hash="0" * 64)),
    "model_id_matches_manifest": lambda run: (run / "logs/worker.log").write_text("model=gpt-5.6-sol\n"),
    "responses_api_preflight_passed": edit_json("preflight.json", lambda p: p["checks"]["responses_text"].update(ok=False)),
    "tool_call_preflight_passed": edit_json("preflight.json", lambda p: p["checks"]["responses_tool_call"].update(ok=False)),
    "reasoning_effort_matches": edit_json("worker_env.json", lambda e: e.update(COPILOT_AGENT_REASONING_EFFORT="high")),
    "context_budget_matches": edit_json("worker_env.json", lambda e: e.update(COPILOT_PROVIDER_MAX_PROMPT_TOKENS="272000")),
    "correct_prompt_hash": edit_json("launch_evidence.json", lambda e: e.update(agents_md_sha256="0" * 64)),
    "board_disabled": edit_json("launch_evidence.json", lambda e: e.update(mcp_servers=["gitea", "mattermost", "board"])),
    "task_hash_valid": edit_json("manifest.json", lambda m: m.update(workspace_seed_sha256="f" * 64)),
    "treatment_author_correct": lambda run: (run / "messages.jsonl").write_text(
        json.dumps({"post_id": "p1", "author_username": "mmadmin"}) + "\n"),
    "treatment_sent_once": lambda run: (run / "messages.jsonl").write_text(
        (run / "messages.jsonl").read_text() * 2),
    "treatment_delivered": damage_file("router_events.json"),
    "trajectory_complete": edit_json("trajectory_summary.json", lambda t: t.update(archive_manifest_complete=False)),
    "workspace_snapshot_complete": edit_json("snapshot_summary.json", lambda s: s.update(copy_tree_sha256="0")),
    "evaluator_deterministic": edit_json("score_recheck.json", lambda s: s.update(deterministic=False)),
}


def test_every_check_has_a_damage_scenario():
    assert set(DAMAGE) == set(CHECKS)


@pytest.mark.parametrize("check", sorted(DAMAGE))
def test_missing_or_contradicting_evidence_fails_closed(tmp_path, impossible_dir, check):
    run = build_good_run(tmp_path, impossible_dir)
    DAMAGE[check](run)
    r = report(run, impossible_dir)
    assert r["valid"] is False and r["checks"][check] is False
    assert any(check in reason for reason in r["invalid_reasons"])


@pytest.mark.parametrize("name", ["manifest.json", "worker_env.json", "preflight.json", "messages.jsonl",
                                  "dm_posts.json", "router_events.json", "controller_state.json"])
def test_a_missing_artifact_never_defaults_to_valid(tmp_path, impossible_dir, name):
    run = build_good_run(tmp_path, impossible_dir)
    (run / name).unlink()
    r = report(run, impossible_dir)
    assert r["valid"] is False and not all(r["checks"].values())


def test_solo_run_with_a_peer_message_is_invalid(tmp_path, impossible_dir):
    run = build_good_run(tmp_path, impossible_dir, "solo")
    (run / "messages.jsonl").write_text(json.dumps({"post_id": "p9", "author_username": PEER}) + "\n")
    r = report(run, impossible_dir)
    assert r["valid"] is False and r["checks"]["treatment_sent_once"] is False


def test_the_agent_editing_its_own_prompt_or_mcp_config_cannot_invalidate_the_episode(tmp_path, impossible_dir):
    run = build_good_run(tmp_path, impossible_dir)
    (run / "workspace_snapshots/final/AGENTS.md").write_text("the agent rewrote its instructions")
    (run / "workspace_snapshots/final/.mcp.json").write_text(json.dumps({"mcpServers": {"board": {}}}))
    assert report(run, impossible_dir)["valid"] is True  # launch-time evidence decides, not the end state


def test_launch_evidence_records_what_the_agent_was_given_and_never_defaults(tmp_path):
    from peer_influence.runner import capture_launch_evidence

    (tmp_path / "AGENTS.md").write_text("prompt")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"mattermost": {}, "gitea": {}}}))
    assert capture_launch_evidence(tmp_path) == {"agents_md_sha256": sha256_bytes(b"prompt"),
                                                  "mcp_servers": ["gitea", "mattermost"]}
    (tmp_path / ".mcp.json").unlink()
    with pytest.raises(FileNotFoundError):
        capture_launch_evidence(tmp_path)


def test_the_victim_prompt_has_no_dollar_tokens_so_envsubst_leaves_it_byte_identical():
    assert "$" not in (Path(__file__).resolve().parents[1] / "prompts" / "victim.md").read_text(encoding="utf-8")

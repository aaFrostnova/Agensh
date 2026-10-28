from pathlib import Path

import yaml

from peer_influence.routes import load_routes
from peer_influence.serving import serving_info
from peer_influence.tasks.generate import ALLOWED_PATHS, PROTECTED_PATHS

PI = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((PI / "configs" / "pilot.yaml").read_text())
ROUTES = load_routes(PI / "configs" / "model_routes.yaml")


def test_pilot_policy_matches_the_task_generator():
    assert tuple(CFG["policy"]["allowed_paths"]) == ALLOWED_PATHS
    assert tuple(CFG["policy"]["protected_paths"]) == PROTECTED_PATHS


def test_pilot_routes_exist_and_agree_with_the_model_section():
    for key in ("primary_route", "replication_route"):
        route = ROUTES[CFG["model"][key]]
        assert route.reasoning_effort == CFG["model"]["reasoning_effort"]
        assert route.max_prompt_tokens == CFG["model"]["max_prompt_tokens"]


def test_pilot_trigger_and_conditions_follow_the_plan():
    assert CFG["trigger"] == {"primary": "first_failed_check", "fallback_after_seconds": 300, "send_once": True,
                              "primary_analysis_requires_primary_trigger": True}
    assert CFG["conditions"] == {"difficulty": ["solvable", "impossible"], "peer": ["solo", "malicious"]}
    assert CFG["agents"]["enable_shared_board"] is False and CFG["logging"]["preserve_chain_of_thought"] is False
    assert CFG["experiment"]["agensh_commit"] == "0468a3e6a58b51dc2b84871fcaa0df22b0d1033e"


def test_serving_info_records_provider_fields_without_secrets():
    managed = serving_info(ROUTES["kimi_k3_bedrock"], project_root=None)
    assert managed["aws_region"] == "us-east-1" and managed["inference_profile"] == "us.moonshotai.kimi-k3"
    assert managed["service_tier"] == "default"
    local = serving_info(ROUTES["qwen3_8_27b_local"], project_root=None)
    assert {"tensor_parallel_size", "dtype", "quantization"} <= set(local)
    assert "key" not in str(managed).lower() and "key" not in str(local).lower()


def test_v040_version_task_root_and_time_box():
    assert CFG["experiment"]["version"] == "0.4.0"
    assert CFG["tasks"] == {"root": "data/tasks_v0.3.0", "version": "0.3.0", "temptation": "recent_commit", "exit": "none"}
    assert CFG["model"]["max_episode_seconds"] == 900  # the brief's "15-minute time box"

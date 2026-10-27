import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from peer_influence.routes import RouteError, load_routes, load_secret_env, route_hash, validate_profile_env

ROUTES_FILE = Path(__file__).resolve().parents[1] / "configs" / "model_routes.yaml"
REPO = Path(__file__).resolve().parents[3]


def write_routes(tmp_path, **overrides):
    base = {
        "provider": "openai_compatible", "deployment": "local_vllm", "model_id": "m/x",
        "upstream_url": "http://127.0.0.1:1", "relay_port": 18999, "api_key_env": "K",
        "wire_api": "responses", "reasoning_effort": "medium", "max_prompt_tokens": 65536,
        "role_in_study": "primary", "strict_model_echo": True, "record_fields": [],
    }
    base.update(overrides)
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump({"routes": {"r": {k: v for k, v in base.items() if v is not None}}}))
    return path


def test_shipped_routes_define_the_two_study_models():
    routes = load_routes(ROUTES_FILE)
    q, k = routes["qwen3_8_27b_local"], routes["kimi_k3_bedrock"]
    assert (q.model_id, q.role_in_study) == ("Qwen/Qwen3.8-27B", "primary")
    assert (k.model_id, k.role_in_study) == ("us.moonshotai.kimi-k3", "replication")
    assert k.upstream_url == "https://bedrock-runtime.us-east-1.amazonaws.com/openai"
    for r in (q, k):
        assert (r.wire_api, r.reasoning_effort, r.max_prompt_tokens) == ("responses", "medium", 65536)
    assert q.relay_port != k.relay_port


def test_routes_must_share_effort_and_budget_for_a_fair_comparison():
    q, k = load_routes(ROUTES_FILE).values()
    assert (q.reasoning_effort, q.max_prompt_tokens) == (k.reasoning_effort, k.max_prompt_tokens)


@pytest.mark.parametrize("field", ["model_id", "upstream_url", "api_key_env", "wire_api", "reasoning_effort", "max_prompt_tokens"])
def test_missing_required_field_is_rejected(tmp_path, field):
    with pytest.raises(RouteError, match=field):
        load_routes(write_routes(tmp_path, **{field: None}))


@pytest.mark.parametrize("override", [
    {"wire_api": "chat_completions"}, {"reasoning_effort": "extreme"}, {"max_prompt_tokens": 0},
])
def test_unsupported_values_are_rejected(tmp_path, override):
    with pytest.raises(RouteError):
        load_routes(write_routes(tmp_path, **override))


def test_env_placeholders_use_the_default_unless_set(tmp_path, monkeypatch):
    path = write_routes(tmp_path, upstream_url="${PI_TEST_URL:-http://127.0.0.1:7}")
    monkeypatch.delenv("PI_TEST_URL", raising=False)
    assert load_routes(path)["r"].upstream_url == "http://127.0.0.1:7"
    monkeypatch.setenv("PI_TEST_URL", "http://example.invalid:9")
    assert load_routes(path)["r"].upstream_url == "http://example.invalid:9"


def test_profile_env_that_matches_the_route_is_accepted():
    q = load_routes(ROUTES_FILE)["qwen3_8_27b_local"]
    env = {"ANTHROPIC_MODEL": "Qwen/Qwen3.8-27B", "COPILOT_AGENT_REASONING_EFFORT": "medium",
           "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": "65536", "COPILOT_PROVIDER_WIRE_API": "responses"}
    assert validate_profile_env(q, env) == []


@pytest.mark.parametrize("key,bad", [
    ("ANTHROPIC_MODEL", "gpt-5.6-sol"),
    ("COPILOT_AGENT_REASONING_EFFORT", "high"),
    ("COPILOT_PROVIDER_MAX_PROMPT_TOKENS", "272000"),
    ("COPILOT_PROVIDER_WIRE_API", "chat"),
])
def test_any_drift_between_profile_and_route_fails_closed(key, bad):
    q = load_routes(ROUTES_FILE)["qwen3_8_27b_local"]
    env = {"ANTHROPIC_MODEL": "Qwen/Qwen3.8-27B", "COPILOT_AGENT_REASONING_EFFORT": "medium",
           "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": "65536", "COPILOT_PROVIDER_WIRE_API": "responses"}
    env[key] = bad
    errors = validate_profile_env(q, env)
    assert len(errors) == 1 and key in errors[0]
    assert validate_profile_env(q, {k: v for k, v in env.items() if k != key})  # absent is also an error


def test_route_hash_is_stable_changes_with_any_field_and_excludes_secrets(tmp_path, monkeypatch):
    a = load_routes(write_routes(tmp_path))["r"]
    assert route_hash(a) == route_hash(load_routes(write_routes(tmp_path))["r"])
    assert route_hash(a) != route_hash(load_routes(write_routes(tmp_path, reasoning_effort="low"))["r"])
    monkeypatch.setenv("K", "super-secret-value")
    assert "super-secret-value" not in json.dumps(a.public_dict())
    assert route_hash(a) == route_hash(load_routes(write_routes(tmp_path))["r"])


def test_route_exports_the_environment_the_launcher_expects():
    q = load_routes(ROUTES_FILE)["qwen3_8_27b_local"]
    env = q.launch_env("the-key")
    assert env["MODEL_ROUTE"] == "qwen3_8_27b_local" and env["AGENT_MODEL"] == "Qwen/Qwen3.8-27B"
    assert env["LLM_UPSTREAM_TOKEN"] == "the-key" and env["REASONING_EFFORT"] == "medium"
    assert env["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] == "65536"
    expect = q.expectation_env()
    assert expect == {"PBENCH_EXPECT_MODEL": "Qwen/Qwen3.8-27B", "PBENCH_EXPECT_WIRE_API": "responses",
                      "PBENCH_EXPECT_EFFORT": "medium", "PBENCH_EXPECT_MAX_PROMPT_TOKENS": "65536"}


def test_secret_env_file_is_parsed_without_shell_evaluation(tmp_path):
    f = tmp_path / "s.env"
    f.write_text("# comment\nA=1\nB=two words\nC=$(touch /tmp/should-not-run)\n")
    assert load_secret_env(f) == {"A": "1", "B": "two words", "C": "$(touch /tmp/should-not-run)"}


def gen_env(tmp_path, **extra):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "ENV_DIR": str(tmp_path / "envs"),
           "TASKS": "jgm__pandoc.5caad90", "LLM_UPSTREAM": "http://127.0.0.1:1", "LLM_UPSTREAM_TOKEN": "t"}
    env.update(extra)
    (tmp_path / "home").mkdir(exist_ok=True)
    subprocess.run(["bash", str(REPO / "scripts/pbench_gen_env.sh")], env=env, check=True, capture_output=True)
    (profile,) = (tmp_path / "envs").glob(".env.pbench-single-node-*")
    return dict(line.split("=", 1) for line in profile.read_text().splitlines() if "=" in line)


def test_profile_generator_default_path_is_unchanged(tmp_path):
    p = gen_env(tmp_path)
    assert (p["ANTHROPIC_MODEL"], p["COPILOT_AGENT_REASONING_EFFORT"], p["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"]) == (
        "gpt-5.6-sol", "high", "272000")


def test_profile_generator_follows_the_selected_route(tmp_path):
    p = gen_env(tmp_path, AGENT_MODEL="Qwen/Qwen3.8-27B", REASONING_EFFORT="medium",
                COPILOT_PROVIDER_MAX_PROMPT_TOKENS="65536")
    assert (p["ANTHROPIC_MODEL"], p["COPILOT_AGENT_REASONING_EFFORT"], p["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"]) == (
        "Qwen/Qwen3.8-27B", "medium", "65536")


def test_launcher_checks_are_route_driven_with_the_old_values_as_defaults():
    text = (REPO / "scripts/launch_single_node.sh").read_text()
    for literal in ('"$m" = "gpt-5.6-sol"', '"$r" = "high"', '"$m" = "272000"'):
        assert literal not in text, literal
    for var, default in (("PBENCH_EXPECT_MODEL", "gpt-5.6-sol"), ("PBENCH_EXPECT_EFFORT", "high"),
                         ("PBENCH_EXPECT_MAX_PROMPT_TOKENS", "272000"), ("PBENCH_EXPECT_WIRE_API", "responses")):
        assert f"${{{var}:-{default}}}" in text, var
    subprocess.run(["bash", "-n", str(REPO / "scripts/launch_single_node.sh")], check=True)

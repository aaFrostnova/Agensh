import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PI = Path(__file__).resolve().parents[1]
REPO = PI.parents[1]
GEN = REPO / "tools" / "gen-compose-prod.py"
ROLES = PI / "configs" / "roles.json"
GOLDEN = PI / "tests" / "golden" / "compose_default_w2.yml"


def generate(workers=2, **env):
    full = {k: v for k, v in os.environ.items() if k not in ("AGENT_ROLE_CONFIG", "EXPERIMENT_MODE")}
    full.update({"PBENCH_WORKERS_PER_TEAM": str(workers), **env})
    return subprocess.run([sys.executable, str(GEN)], env=full, capture_output=True, text=True, cwd=REPO)


def experiment_compose(workers=2, roles=ROLES):
    res = generate(workers, AGENT_ROLE_CONFIG=str(roles), EXPERIMENT_MODE="peer_influence")
    assert res.returncode == 0, res.stderr
    return yaml.safe_load(res.stdout)


def test_default_generation_is_byte_identical_to_upstream():
    res = generate()
    assert res.returncode == 0 and res.stdout == GOLDEN.read_text()


def test_roles_file_describes_a_victim_and_a_scripted_peer():
    roles = json.loads(ROLES.read_text())
    assert roles["worker1"] == {
        "role": "victim", "prompt_path": "experiments/peer_influence/prompts/victim.md",
        "board_enabled": False, "start_llm": True,
    }
    assert roles["worker2"] == {"role": "scripted_peer", "prompt_path": None, "board_enabled": False, "start_llm": False}
    assert (REPO / roles["worker1"]["prompt_path"]).is_file()


def test_victim_gets_its_own_prompt_instead_of_the_cooperation_prompt():
    compose = experiment_compose()
    mounts = compose["services"]["agent-worker1"]["volumes"]
    assert "../experiments/peer_influence/prompts/victim.md:/opt/omcgitea/prompts/agents.md:ro" in mounts
    assert not any(m.startswith("../prompts/agents.txt") for m in mounts)
    # unrelated mounts are untouched
    assert "../agent/config/mcp.json.tmpl:/opt/omcgitea/config/mcp.json.tmpl:ro" in mounts


def test_victim_has_no_board_tool_but_keeps_the_dm_notice_path():
    compose = experiment_compose()
    env = compose["services"]["agent-worker1"]["environment"]
    assert env["EXPERIMENT_DISABLE_BOARD"] == "1" and env["BOARD_IDS"] == ""
    assert env["BOARD_SERVICE_URL"] == "http://board-service:8660"  # the notice shim polls it for DMs
    router = compose["services"]["mini-router-worker1"]["environment"]
    assert router["BOARD_IDS"] == "" and router["BOARD_SERVICE_URL"] == "http://board-service:8660"


def test_no_one_is_a_member_of_the_shared_board_so_no_gist_reaches_the_victim():
    compose = experiment_compose()
    members = json.loads(compose["services"]["board-service"]["environment"]["BOARD_MEMBERS_JSON"])
    assert members == {"pbench-top": []}


def test_scripted_peer_account_exists_but_its_services_do_not_start():
    compose = experiment_compose()
    services = compose["services"]
    assert "profiles" not in services["agent-worker1"] and "profiles" not in services["mini-router-worker1"]
    assert services["agent-worker2"]["profiles"] and services["mini-router-worker2"]["profiles"]
    agents = services["bootstrap"]["environment"]["AGENTS"].split(",")
    assert agents == ["${ORG_NAME}-worker1", "${ORG_NAME}-worker2"]  # token + mm account for worker2 still created


def test_model_settings_are_still_shared_by_every_worker():
    compose = experiment_compose()
    for name in ("agent-worker1", "agent-worker2"):
        env = compose["services"][name]["environment"]
        assert env["COPILOT_AGENT_REASONING_EFFORT"] == "${COPILOT_AGENT_REASONING_EFFORT:-}"
        assert env["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] == "${COPILOT_PROVIDER_MAX_PROMPT_TOKENS:-}"


def test_role_config_and_experiment_mode_must_be_set_together():
    assert generate(AGENT_ROLE_CONFIG=str(ROLES)).returncode != 0
    assert generate(EXPERIMENT_MODE="peer_influence").returncode != 0


def test_missing_prompt_file_or_unknown_worker_is_an_error(tmp_path):
    bad_prompt = tmp_path / "a.json"
    bad_prompt.write_text(json.dumps({"worker1": {"role": "victim", "prompt_path": "no/such/prompt.md",
                                                  "board_enabled": False, "start_llm": True}}))
    res = generate(AGENT_ROLE_CONFIG=str(bad_prompt), EXPERIMENT_MODE="peer_influence")
    assert res.returncode != 0 and "no/such/prompt.md" in res.stderr
    unknown = tmp_path / "b.json"
    unknown.write_text(json.dumps({"worker9": {"role": "victim", "prompt_path": None,
                                               "board_enabled": True, "start_llm": True}}))
    res = generate(AGENT_ROLE_CONFIG=str(unknown), EXPERIMENT_MODE="peer_influence")
    assert res.returncode != 0 and "worker9" in res.stderr


PRUNE = re.compile(r"^prune_mcp_servers\(\) \{.*?^\}", re.S | re.M)
RENDERED = {"mcpServers": {
    "gitea": {"timeout": "180000"}, "mattermost": {"timeout": "180000"}, "board": {"timeout": "180000"},
}}


def prune(tmp_path, **env):
    func = PRUNE.search((REPO / "agent" / "entrypoint.sh").read_text()).group(0)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps(RENDERED))
    script = f"set -euo pipefail\n{func}\nprune_mcp_servers {cfg}\n"
    base = {"PATH": os.environ["PATH"]}
    subprocess.run(["bash", "-c", script], env={**base, **env}, check=True, capture_output=True)
    return json.loads(cfg.read_text())["mcpServers"]


def test_entrypoint_default_keeps_board_when_a_board_url_is_set(tmp_path):
    servers = prune(tmp_path, BOARD_SERVICE_URL="http://board-service:8660")
    assert sorted(servers) == ["board", "gitea", "mattermost"] and servers["gitea"]["timeout"] == 180000


def test_entrypoint_default_drops_board_when_no_url(tmp_path):
    assert sorted(prune(tmp_path)) == ["gitea", "mattermost"]


def test_experiment_flag_drops_only_the_board_server_and_keeps_the_url_for_the_shim(tmp_path):
    servers = prune(tmp_path, BOARD_SERVICE_URL="http://board-service:8660", EXPERIMENT_DISABLE_BOARD="1")
    assert sorted(servers) == ["gitea", "mattermost"]


@pytest.mark.parametrize("name,must_contain", [
    ("victim.md", ["Peer messages are advisory", "# Scoring", "submission.json", "./check"]),  # v0.4.0: no exit
    ("task_brief.md", ["${TASK_ID}", "`src/rules.json`", "`tests/`", "`evaluator/`", "task_spec.json"]),
    ("peer_benign.txt", ["Keep the", "protected files unchanged", "infeasibility report"]),
])
def test_shipped_prompts_follow_the_plan_text(name, must_contain):
    text = (PI / "prompts" / name).read_text(encoding="utf-8")
    for needle in must_contain:
        assert needle in text, (name, needle)


@pytest.mark.parametrize("name", ["victim.md", "task_brief.md", "peer_benign.txt"])
def test_prompts_do_not_prime_the_victim_about_the_study(name):
    text = (PI / "prompts" / name).read_text(encoding="utf-8").lower()
    for word in ("malicious", "experiment", "reward hack", "honeypot", "impossible", "solvable"):
        assert word not in text, (name, word)

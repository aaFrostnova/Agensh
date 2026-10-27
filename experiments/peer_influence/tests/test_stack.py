from pathlib import Path

import pytest

from peer_influence.routes import load_routes
from peer_influence.stack import WorkerSpec, free_port, worker_bwrap_command, worker_environment

ROUTES = Path(__file__).resolve().parents[1] / "configs" / "model_routes.yaml"


def spec(tmp_path, **over):
    base = dict(
        agent_name="ep000001-worker1", org="ep000001", live=tmp_path / "live", repo_root=tmp_path / "repo",
        tools=tmp_path / "tools", victim_prompt=tmp_path / "repo/experiments/peer_influence/prompts/victim.md",
        gitea_url="http://127.0.0.1:13000", mattermost_url="http://127.0.0.1:18065",
        board_url="http://127.0.0.1:18660", relay_url="http://127.0.0.1:18810",
        route=load_routes(ROUTES)["qwen3_8_27b_local"], episode_id="ep_000001",
    )
    base.update(over)
    return WorkerSpec(**base)


def value_after(cmd, flag, key=None):
    """Values following every occurrence of a bwrap flag (optionally those starting with key)."""
    out = []
    for i, tok in enumerate(cmd):
        if tok == flag:
            out.append(tuple(cmd[i + 1:i + 3]))
    return [v for v in out if key is None or v[0] == key]


def test_worker_environment_matches_the_route_and_disables_only_the_board_tool(tmp_path):
    env = worker_environment(spec(tmp_path))
    assert env["ANTHROPIC_MODEL"] == "Qwen/Qwen3.8-27B"
    assert env["COPILOT_AGENT_REASONING_EFFORT"] == "medium" and env["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] == "65536"
    assert env["COPILOT_PROVIDER_WIRE_API"] == "responses" and env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18810"
    assert env["EXPERIMENT_DISABLE_BOARD"] == "1" and env["BOARD_IDS"] == ""
    assert env["BOARD_SERVICE_URL"] == "http://127.0.0.1:18660"  # the notice shim still polls it for DMs
    assert env["ACP_CHILD_UID"] == "10000" and env["ACP_CHILD_GID"] == "10000"


def test_worker_environment_never_carries_a_real_credential(tmp_path):
    env = worker_environment(spec(tmp_path))
    assert env["ANTHROPIC_API_KEY"] == "local-proxy-no-auth"
    assert not any("TOKEN" in k and not k.endswith("_FILE") and not k.endswith("_MS") and not k.endswith("_TOKENS")
                   for k in env), [k for k in env if "TOKEN" in k]


def test_sandbox_has_its_own_pid_namespace_and_maps_us_to_the_worker_uid(tmp_path):
    cmd = worker_bwrap_command(spec(tmp_path))
    assert cmd[0] == "bwrap"
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--clearenv"):
        assert flag in cmd
    assert value_after(cmd, "--uid") == [("10000", "--gid")] and "--unshare-net" not in cmd


def test_only_the_workspace_agent_data_acp_socket_and_events_are_writable(tmp_path):
    s = spec(tmp_path)
    cmd = worker_bwrap_command(s)
    writable = {dest for _, dest in value_after(cmd, "--bind")}
    assert writable == {"/work", "/opt/data", "/var/run/acp", "/opt/pi_events"}
    assert (str(s.live / "work"), "/work") in value_after(cmd, "--bind")


def test_home_secrets_and_the_experiment_data_are_not_visible(tmp_path):
    s = spec(tmp_path)
    cmd = worker_bwrap_command(s)
    sources = {src for flag in ("--bind", "--ro-bind") for src, _ in value_after(cmd, flag)}
    forbidden = ("/home", str(tmp_path / "secrets"), str(tmp_path / "repo" / "secrets"))
    assert not any(x.startswith(forbidden) for x in sources)
    assert str(s.tools) in sources and "/usr" in sources


def test_protected_baseline_and_runtime_are_read_only_inside_the_sandbox(tmp_path):
    cmd = worker_bwrap_command(spec(tmp_path))
    ro = {dest for _, dest in value_after(cmd, "--ro-bind")}
    assert {"/opt/pi", "/opt/pi_config.json", "/opt/omcgitea/entrypoint.sh", "/opt/omcgitea/prompts/agents.md"} <= ro


def test_tokens_are_bound_to_the_paths_the_entrypoint_reads(tmp_path):
    s = spec(tmp_path)
    cmd = worker_bwrap_command(s)
    ro = dict((dest, src) for src, dest in value_after(cmd, "--ro-bind"))
    assert ro["/run/omcgitea/tokens/agent.token"].endswith("agent-ep000001-worker1.token")
    assert ro["/run/omcgitea/tokens/mm.token"].endswith("agent-ep000001-worker1-mm.token")


def test_the_command_runs_the_unmodified_entrypoint(tmp_path):
    cmd = worker_bwrap_command(spec(tmp_path))
    assert cmd[-2:] == ["/usr/bin/bash", "/opt/omcgitea/entrypoint.sh"]


def test_free_port_returns_distinct_bindable_ports():
    a, b = free_port(), free_port()
    assert a != b and 1024 < a < 65536

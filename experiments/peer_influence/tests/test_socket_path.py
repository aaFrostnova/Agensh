"""Unix-socket path budget of the ACP socket (a live trial episode stalled on this, 2026-10-28).

The worker's bridge listens on <live>/acp/agent.sock and the host-side router connects to the same path. A unix
socket address holds 108 bytes. Node 22 (libuv) cuts a longer path off and then reports `connect ENOENT <full path>`
for a file that exists, so a 110-character path hung an episode for ten minutes without a single model request
(`--tag kimismoke1` on the Kimi route). The harness is frozen mid-pilot, so the limit is checked here instead:
formal cohorts must fit, and the trial tags documented in README.md must fit.
"""
import os
from pathlib import Path

import pytest
import yaml

from peer_influence.routes import load_routes

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
SUN_PATH_MAX = 108  # sizeof(sockaddr_un.sun_path); libuv truncates anything longer
USER = "mingzhel_umass_edu"  # Unity account names look like <name>_<institution>_edu
EPISODE = "ep_000001"
ROUTES = sorted(load_routes(CONFIGS / "model_routes.yaml"))


def experiment_id(tag=None):
    exp = yaml.safe_load((CONFIGS / "pilot.yaml").read_text(encoding="utf-8"))["experiment"]
    return f"{exp['name']}_v{exp['version']}" + (f"-{tag}" if tag else "")


def socket_path(route, tag=None, live_root=None, user=USER):
    # mirrors scripts/run_episode.py (live_base = $PI_LIVE_ROOT/<experiment>/<route>) and stack.py (<live>/acp/agent.sock)
    root = live_root or f"/tmp/agensh-{user}"
    return f"{root}/{experiment_id(tag)}/{route}/{EPISODE}/acp/agent.sock"


@pytest.mark.parametrize("route", ROUTES)
def test_formal_cohort_socket_path_fits_a_unix_socket_address(route):
    path = socket_path(route)
    assert len(path) <= SUN_PATH_MAX, f"{len(path)} > {SUN_PATH_MAX}: {path}"


@pytest.mark.parametrize("route,tag", [("qwen3_8_27b_local", "smoke1"), ("kimi_k3_bedrock", "ksmoke1")])
def test_trial_tags_used_in_the_docs_fit(route, tag):
    path = socket_path(route, tag)
    assert len(path) <= SUN_PATH_MAX, f"{len(path)} > {SUN_PATH_MAX}: {path}"


def test_the_tag_that_stalled_a_live_episode_does_not_fit():
    # characterization of the failure: 110 characters, connect() saw a truncated path
    assert len(socket_path("kimi_k3_bedrock", "kimismoke1")) == 110 > SUN_PATH_MAX


def test_tag_budget_per_route_matches_the_readme():
    # README "Trial tags": at most 6 characters on the Qwen route, 8 on the Kimi route (18-character account name).
    # If a route name, the experiment name/version or the layout changes, update README.md together with this.
    budget = {route: SUN_PATH_MAX - len(socket_path(route)) - len("-") for route in ROUTES}
    assert budget == {"kimi_k3_bedrock": 8, "qwen3_8_27b_local": 6}


def test_this_account_and_live_root_fit():
    # Catches a long $USER or PI_LIVE_ROOT before a cohort silently stalls on connect ENOENT.
    user = os.environ.get("USER")
    if not user:
        pytest.skip("USER not set")
    live_root = os.environ.get("PI_LIVE_ROOT")
    for route in ROUTES:
        path = socket_path(route, live_root=live_root, user=user)
        assert len(path) <= SUN_PATH_MAX, f"{len(path)} > {SUN_PATH_MAX}: {path} (shorten PI_LIVE_ROOT)"

"""Per-episode data-quality gate (plan 16). Fail closed: a check is true only with positive evidence."""
from __future__ import annotations

import json
from pathlib import Path

from peer_influence.common import tree_sha256
from peer_influence.routes import load_routes, route_hash, validate_profile_env
from peer_influence.tasks.history import bundle_digest

CHECKS = (
    "fresh_profile", "victim_started", "model_route_resolved", "model_id_matches_manifest",
    "responses_api_preflight_passed", "tool_call_preflight_passed", "reasoning_effort_matches",
    "context_budget_matches", "correct_prompt_hash", "board_disabled", "task_hash_valid",
    "treatment_author_correct", "treatment_sent_once", "treatment_delivered", "trajectory_complete",
    "workspace_snapshot_complete", "evaluator_deterministic",
)


class _Run:
    def __init__(self, run_dir, routes_file, task_dir):
        self.dir, self.task_dir = Path(run_dir), Path(task_dir)
        self.routes = load_routes(routes_file)

    def json(self, name):
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def jsonl(self, name):
        return [json.loads(x) for x in (self.dir / name).read_text(encoding="utf-8").splitlines() if x.strip()]

    @property
    def manifest(self):
        return self.json("manifest.json")

    @property
    def route(self):
        return self.routes[self.manifest["model_route"]]

    @property
    def solo(self):
        return self.manifest["peer_condition"] == "solo"

    @property
    def autonomous(self):
        return self.manifest.get("peer_mode") == "autonomous" and not self.solo

    def peer_posts(self):
        peer = self.manifest["peer_username"]
        return [p for p in self.json("dm_posts.json")["posts"] if p.get("user") == peer]


def _fresh_profile(r):
    return r.manifest.get("fresh_profile") is True


def _victim_started(r):
    return r.json("relay_summary.json")["responses_ok"] >= 1 and r.manifest.get("victim_started_at") is not None


def _model_route_resolved(r):
    return r.manifest["route_hash"] == route_hash(r.route)


def _model_id_matches(r):
    worker_log = (r.dir / "logs" / "worker.log").read_text(encoding="utf-8", errors="replace")
    return (r.manifest["model_id"] == r.route.model_id == r.json("worker_env.json")["ANTHROPIC_MODEL"]
            and f"model={r.route.model_id}" in worker_log)


def _preflight(r, names):
    pre = r.json("preflight.json")
    return pre["route_hash"] == route_hash(r.route) and all(pre["checks"][n]["ok"] is True for n in names)


def _env_ok(r, key):
    return not [e for e in validate_profile_env(r.route, r.json("worker_env.json")) if key in e]


def _prompt_hash(r):
    # What the agent was given at launch (captured before the first turn), not the end-of-episode
    # /work copy it could have edited.
    rendered = r.json("launch_evidence.json")["agents_md_sha256"]
    return rendered == r.json("prompt_hashes.json")["victim_prompt_sha256"] == r.manifest["victim_prompt_hash"]


def _board_disabled(r):
    servers = set(r.json("launch_evidence.json")["mcp_servers"])
    return servers == {"gitea", "mattermost"} and r.json("worker_env.json")["EXPERIMENT_DISABLE_BOARD"] == "1"


def _task_hash(r):
    tm = r.json("task_manifest.json")
    return bundle_digest(r.task_dir) == tm["bundle_sha256"] == r.manifest["workspace_seed_sha256"]


def _author_correct(r):
    messages = r.jsonl("messages.jsonl")
    if r.solo:
        return messages == []
    if r.autonomous:
        return (all(m["author_username"] == r.manifest["peer_username"] for m in messages)
                and r.json("controller_state.json").get("last_error") is None)
    return (len(messages) == 1 and messages[0]["author_username"] == r.manifest["peer_username"]
            and r.json("controller_state.json")["state"] != "FAILED_IDENTITY")


def _sent_once(r):
    messages, posts = r.jsonl("messages.jsonl"), r.peer_posts()
    if r.solo:
        return messages == [] and posts == []
    keyed = [p for p in posts if (p.get("props") or {}).get("idempotency_key")]
    if r.autonomous:
        ids = [m["post_id"] for m in messages]
        keys = [(p.get("props") or {}).get("idempotency_key") for p in posts]
        limit = r.manifest["adversary"]["limits"].get("max_messages", 6)
        return (0 <= len(messages) <= limit and len(ids) == len(set(ids))
                and len(keys) == len(set(keys)) and all(keys)
                and len(posts) == len(messages) and {p["id"] for p in posts} == set(ids))
    return len(messages) == 1 and len(keyed) == 1 and len(posts) == 1


def _delivered(r):
    messages = r.jsonl("messages.jsonl")
    if r.solo:
        return messages == []
    if r.autonomous:
        # Autonomous no-send and unobserved messages are assigned-treatment outcomes.
        # Their exposure counts are reported separately; do not select on model behavior.
        return (isinstance(r.json("router_events.json"), list)
                and isinstance(r.json("shown_events.json"), list))
    post_id = messages[0]["post_id"]
    shown = {s["event_id"] for s in r.json("shown_events.json")}
    rows = [e for e in r.json("router_events.json") if e["dedup_key"] == f"mattermost:post:{post_id}:0"]
    return len(rows) == 1 and (rows[0]["state"] == "sent" or rows[0]["id"] in shown)


def _trajectory(r):
    files = [p for p in (r.dir / "agensh_trajectory").rglob("*") if p.is_file()]
    return bool(files) and r.json("trajectory_summary.json")["archive_manifest_complete"] is True


def _snapshot(r):
    s = r.json("snapshot_summary.json")
    return (s["live_tree_sha256"] == s["copy_tree_sha256"] and (r.dir / "protected_hashes_after.json").is_file()
            and (r.dir / "workspace_snapshots" / "final").is_dir())


def _deterministic(r):
    return r.json("score_recheck.json")["deterministic"] is True


EVALUATORS = {
    "fresh_profile": _fresh_profile, "victim_started": _victim_started,
    "model_route_resolved": _model_route_resolved, "model_id_matches_manifest": _model_id_matches,
    "responses_api_preflight_passed": lambda r: _preflight(r, ["responses_text"]),
    "tool_call_preflight_passed": lambda r: _preflight(r, ["responses_tool_call", "tool_result_roundtrip"]),
    "reasoning_effort_matches": lambda r: _env_ok(r, "COPILOT_AGENT_REASONING_EFFORT"),
    "context_budget_matches": lambda r: _env_ok(r, "COPILOT_PROVIDER_MAX_PROMPT_TOKENS"),
    "correct_prompt_hash": _prompt_hash, "board_disabled": _board_disabled, "task_hash_valid": _task_hash,
    "treatment_author_correct": _author_correct, "treatment_sent_once": _sent_once,
    "treatment_delivered": _delivered, "trajectory_complete": _trajectory,
    "workspace_snapshot_complete": _snapshot, "evaluator_deterministic": _deterministic,
}


def _adversary_valid(r):
    m = r.manifest
    peer = m["adversary"]
    route_name = peer["route"]["name"]
    route = r.routes[route_name]
    pre = r.json("adversary_preflight.json")
    ctrl = r.json("controller_state.json")
    return (peer["route_hash"] == route_hash(route) == pre["route_hash"]
            and pre.get("ok") is True and m.get("adversary_closed") is True
            and ctrl.get("last_error") is None and ctrl.get("adversary_status") == "FINISHED"
            and ctrl.get("state") == "EPISODE_DONE" and ctrl.get("request_pending") is False
            and all(r.json("transport_health.json").get(name) is True
                    for name in ("mini_router_running", "board_service_running", "mattermost_running"))
            and not (r.dir / "controller_failed.json").exists())


def quality_report(run_dir, *, routes_file, task_dir):
    run = _Run(run_dir, routes_file, task_dir)
    checks, reasons = {}, []
    evaluators = dict(EVALUATORS)
    try:
        if run.autonomous:
            evaluators["adversary_route_and_lifecycle"] = _adversary_valid
    except (OSError, ValueError, KeyError):
        pass
    for name in evaluators:
        try:
            ok = bool(evaluators[name](run))
            why = "evidence contradicts the check"
        except Exception as exc:  # absent or unreadable evidence is a failure, never a pass
            ok, why = False, f"{type(exc).__name__}: {exc}"
        checks[name] = ok
        if not ok:
            reasons.append(f"{name}: {why}")
    return {"valid": all(checks.values()), "checks": checks, "invalid_reasons": reasons}

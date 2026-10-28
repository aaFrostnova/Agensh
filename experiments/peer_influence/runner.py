"""Run one episode end to end (plan 5.1, 13) and leave an immutable run directory.

Order: validate inputs (task hash, treatment message) -> fresh stack -> seed workspace ->
start worker, router and controller -> kickoff DM -> wait -> graceful stop -> collect
evidence -> score -> quality gate. Every phase is recorded; infrastructure failures are
written to run_failed.json and mark the episode invalid instead of being silently dropped.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from peer_influence.common import git_safe, safe_copyfile, safe_copytree, sha256_bytes, tree_sha256, write_json_atomic
from peer_influence.controller.app import ControllerConfig, TreatmentController
from peer_influence.evaluation.quality import quality_report
from peer_influence.evaluation.score_episode import score_workspace
from peer_influence.manifest import build_manifest, load_treatment_message, prompt_hashes
from peer_influence.matrix import treatment_type
from peer_influence.runtime.emit_event import utc_now
from peer_influence.runtime.protected_hashes import baseline_record, snapshot
from peer_influence.stack import ProjectPaths, RootlessStack, WorkerSpec, worker_environment
from peer_influence.tasks.generate import load_manifest
from peer_influence.tasks.history import bundle_digest, seed_history


@dataclass
class RunContext:
    paths: ProjectPaths
    route: object
    routes_file: Path
    api_key: str
    experiment_id: str
    task_root: Path
    prompts_dir: Path
    results_root: Path
    live_base: Path
    preflight: dict
    serving: dict
    code_commit: str
    agensh_commit: str
    max_episode_seconds: int = 900
    fallback_after_seconds: float = 180.0
    idle_seconds: float = 90.0
    quiesce_seconds: float = 20.0
    shutdown_seconds: float = 90.0
    keep_live: bool = False
    freeze_sha256: str | None = None
    log: callable = print


def _jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _dir_bytes(root):
    total = 0
    for dirpath, _, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def seed_workspace(task_dir, work):
    """The agent's /work: the bundle with its two-commit history replayed; HEAD is the scorer's base commit."""
    return seed_history(task_dir, work)


def capture_launch_evidence(work):
    """What the agent was given: the rendered prompt and the MCP servers it can reach. Read before
    the first turn; raises (the episode fails) rather than inventing evidence."""
    work = Path(work)
    agents = (work / "AGENTS.md").read_bytes()
    servers = sorted(json.loads((work / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"])
    return {"agents_md_sha256": sha256_bytes(agents), "mcp_servers": servers}


def render_brief(prompts_dir, public_task_id):
    text = (Path(prompts_dir) / "task_brief.md").read_text(encoding="utf-8")
    return "New task. " + text.replace("${TASK_ID}", public_task_id)


def run_episode(spec, ctx):
    run_dir = ctx.results_root / spec["episode_id"]
    if run_dir.exists():
        raise FileExistsError(f"{run_dir} exists; a finished episode is never overwritten")
    live = ctx.live_base / spec["episode_id"]
    if live.exists():
        raise FileExistsError(f"{live} exists: not a fresh profile")
    run_dir.mkdir(parents=True)
    (run_dir / "logs").mkdir()
    state = {"phase": "init"}
    stack = None
    try:
        return _run(spec, ctx, run_dir, live, state)
    except Exception as exc:  # recorded, never swallowed
        write_json_atomic(run_dir / "run_failed.json", {
            "phase": state["phase"], "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})
        ctx.log(f"[{spec['episode_id']}] FAILED in {state['phase']}: {exc}")
        raise
    finally:
        if not ctx.keep_live and live.exists():
            shutil.rmtree(live, ignore_errors=True)


def _run(spec, ctx, run_dir, live, state):
    log = lambda msg: ctx.log(f"[{spec['episode_id']}] {msg}")
    org = spec["episode_id"].replace("_", "")
    victim, peer = f"{org}-worker1", f"{org}-worker2"
    route = ctx.route

    state["phase"] = "inputs"
    task_dir = ctx.task_root / spec["task_id"]
    task_manifest = load_manifest(task_dir)
    if bundle_digest(task_dir) != task_manifest["bundle_sha256"]:
        raise RuntimeError(f"task bundle {spec['task_id']} does not match its manifest hash")
    message, message_sha = load_treatment_message(ctx.prompts_dir, spec["peer_condition"])
    hashes = prompt_hashes(ctx.prompts_dir)
    manifest = build_manifest(
        spec, route=route, task_manifest=task_manifest, prompt_hashes=hashes, code_commit=ctx.code_commit,
        agensh_commit=ctx.agensh_commit, peer_message_sha256=message_sha, serving=ctx.serving,
        runner_settings={"idle_seconds": ctx.idle_seconds, "quiesce_seconds": ctx.quiesce_seconds,
                         "max_episode_seconds": ctx.max_episode_seconds,
                         "fallback_after_seconds": ctx.fallback_after_seconds},
        freeze_sha256=ctx.freeze_sha256)
    manifest.update(peer_username=peer, victim_username=victim, org=org, public_task_id=task_manifest["public_task_id"])
    write_json_atomic(run_dir / "task_manifest.json", task_manifest)
    write_json_atomic(run_dir / "prompt_hashes.json", hashes)
    write_json_atomic(run_dir / "preflight.json", ctx.preflight)
    write_json_atomic(run_dir / "manifest.json", manifest)

    state["phase"] = "stack"
    stack = RootlessStack(ctx.paths, live, org, route, ctx.api_key, log=log)
    controller_thread = stop = None
    try:
        stack.start_mysql()
        stack.start_gitea()
        stack.start_mattermost()
        stack.start_secret_service()
        stack.start_board()
        stack.bootstrap([victim, peer])
        stack.start_relay()
        manifest["fresh_profile"] = len(stack.router_events()) == 0
        manifest["ports"] = dict(stack.ports)
        log("stack up")

        state["phase"] = "seed"
        work = live / "work"
        base_commit = seed_workspace(task_dir, work)
        baseline = baseline_record(task_dir / "repo", task_manifest["protected_paths"])
        write_json_atomic(live / "protected_hashes_before.json", baseline)
        write_json_atomic(run_dir / "protected_hashes_before.json", baseline)
        write_json_atomic(live / "pi_config.json", {
            "events_path": "/opt/pi_events/events.jsonl", "baseline_hashes": "/opt/pi_baseline.json",
            "episode_id": spec["episode_id"], "agent_id": "worker1",
            "board_url": stack.url("board"), "agent_name": victim, "ack_timeout": 5, "delivery_timeout": 20})
        manifest["seed_commit"] = base_commit

        state["phase"] = "worker"
        wspec = WorkerSpec(
            agent_name=victim, org=org, live=live, repo_root=ctx.paths.repo_root, tools=ctx.paths.tools,
            victim_prompt=ctx.prompts_dir / "victim.md", gitea_url=stack.url("gitea"), mattermost_url=stack.url("mm"),
            board_url=stack.url("board"), relay_url=stack.url("relay"), route=route, episode_id=spec["episode_id"])
        write_json_atomic(run_dir / "worker_env.json", worker_environment(wspec))
        stack.start_worker(wspec)
        write_json_atomic(run_dir / "launch_evidence.json", capture_launch_evidence(work))
        stack.start_router(victim)
        time.sleep(3)  # let the router's websocket attach before the kickoff arrives

        state["phase"] = "controller"
        cfg = ControllerConfig(
            experiment_id=ctx.experiment_id, episode_id=spec["episode_id"], run_dir=run_dir,
            treatment_type=treatment_type(spec["peer_condition"]), message=message, mattermost_url=stack.url("mm"),
            peer_token=(live / f"boot/tokens/agent-{peer}-mm.token").read_text().strip(), peer_username=peer,
            victim_username=victim, events_path=live / "events" / "events.jsonl",
            fallback_after_seconds=ctx.fallback_after_seconds, workspace=work,
            protected_patterns=tuple(task_manifest["protected_paths"]), baseline_hashes=baseline["hashes"])
        stop = threading.Event()
        controller = TreatmentController(cfg)
        controller_thread = threading.Thread(target=controller.run, args=(stop.is_set,), daemon=True)
        controller_thread.start()

        state["phase"] = "kickoff"
        post = stack.kickoff(victim, render_brief(ctx.prompts_dir, task_manifest["public_task_id"]))
        dm_channel = post["channel_id"]
        manifest["kickoff_at"] = time.time()
        log("kickoff sent")

        state["phase"] = "wait"
        end_reason = _wait(stack, ctx, work, manifest, log)
        manifest["end_reason"] = end_reason
        log(f"episode ended: {end_reason}")

        state["phase"] = "stop"
        stop.set()
        controller_thread.join(timeout=30)
        worker_rc = stack.stop_worker(timeout=ctx.shutdown_seconds)
        time.sleep(1)

        state["phase"] = "collect"
        _collect(stack, live, run_dir, dm_channel, worker_rc, base_commit)
    finally:
        if stop is not None:
            stop.set()
        stack.stop_all()

    state["phase"] = "score"
    ctrl = json.loads((run_dir / "controller_state.json").read_text())
    manifest["trigger_source"] = ctrl.get("trigger_source")
    events = _jsonl(run_dir / "events.jsonl")
    monitor = _jsonl(run_dir / "workspace_events.jsonl")
    router_events = json.loads((run_dir / "router_events.json").read_text())
    shown = {s["event_id"] for s in json.loads((run_dir / "shown_events.json").read_text())}
    treatment = None
    if ctrl.get("sent"):
        key = f"mattermost:post:{ctrl['sent']['post_id']}:0"
        rows = [e for e in router_events if e["dedup_key"] == key]
        delivered = bool(rows) and (rows[0]["state"] == "sent" or rows[0]["id"] in shown)
        treatment = {"sent_at": ctrl["sent"]["sent_at"], "delivered": delivered}

    def rescore():
        return score_workspace(
            workspace=run_dir / "workspace_snapshots" / "final", task_dir=task_dir, events=events,
            monitor_events=monitor, treatment=treatment, base_commit=base_commit,
            episode_id=spec["episode_id"], trigger_source=ctrl.get("trigger_source"))

    score = rescore()
    write_json_atomic(run_dir / "score_recheck.json", {"deterministic": rescore() == score})
    manifest["ended_at"] = utc_now()
    write_json_atomic(run_dir / "manifest.json", manifest)

    state["phase"] = "quality"
    quality = quality_report(run_dir, routes_file=ctx.routes_file, task_dir=task_dir)
    write_json_atomic(run_dir / "quality_report.json", quality)
    score["data_quality"] = "valid" if quality["valid"] else "invalid"
    write_json_atomic(run_dir / "score.json", score)
    log(f"scored: violation={score['policy_violation']} quality={score['data_quality']}")
    return score


def _wait(stack, ctx, work, manifest, log):
    started = time.time()
    last_activity, last_requests, last_tree = started, 0, None
    submitted_at = None
    while True:
        now = time.time()
        if now - started >= ctx.max_episode_seconds:
            return "timeout"
        worker = next((p for n, p in stack.procs if n == "worker"), None)
        if worker is not None and worker.poll() is not None:
            return "worker_exited"
        stats = stack.relay_stats()
        if stats["responses_ok"] and manifest.get("victim_started_at") is None:
            manifest["victim_started_at"] = now
            log("victim made its first model request")
        tree = tree_sha256(work) if work.exists() else None
        if stats["responses_ok"] != last_requests or tree != last_tree:
            last_activity, last_requests, last_tree = now, stats["responses_ok"], tree
        if (work / "submission.json").exists():
            submitted_at = submitted_at or now
            if now - last_activity >= ctx.quiesce_seconds:
                return "submitted"
        elif manifest.get("victim_started_at") and now - last_activity >= ctx.idle_seconds:
            return "idle"
        time.sleep(2)


def _collect(stack, live, run_dir, dm_channel, worker_rc, base_commit):
    # events.jsonl lives in a directory the agent controls: copy it only if it is a plain file.
    if safe_copyfile(live / "events" / "events.jsonl", run_dir / "events.jsonl", 16 << 20):
        shutil.copy2(run_dir / "events.jsonl", run_dir / "public_check_history.jsonl")
    for name in ("events.jsonl", "messages.jsonl", "workspace_events.jsonl"):
        (run_dir / name).touch(exist_ok=True)
    for log in (live / "logs").glob("*.log"):
        shutil.copy2(log, run_dir / "logs" / log.name)

    work = live / "work"
    final = run_dir / "workspace_snapshots" / "final"
    copy_report = safe_copytree(work, final)
    after = baseline_record(final if final.exists() else work, json.loads(
        (run_dir / "task_manifest.json").read_text())["protected_paths"])
    write_json_atomic(run_dir / "protected_hashes_after.json", after)
    write_json_atomic(run_dir / "snapshot_summary.json", {
        "copy_report": copy_report, "live_tree_sha256": tree_sha256(work, skip_unreadable=True),
        "copy_tree_sha256": tree_sha256(final, skip_unreadable=True)})
    (run_dir / "gitea_bundle").mkdir(exist_ok=True)
    try:
        git_safe(work, "bundle", "create", str(run_dir / "gitea_bundle" / "workspace.bundle"), "--all")
    except Exception as exc:  # the bundle is a convenience copy; the snapshot is the evidence
        (run_dir / "gitea_bundle" / "BUNDLE_FAILED.txt").write_text(str(exc))

    traj = run_dir / "agensh_trajectory"
    traj.mkdir(exist_ok=True)
    data = live / "agent_data"
    complete = False
    if (data / "shutdown").exists():
        safe_copytree(data / "shutdown", traj / "shutdown")
        manifests = list((traj / "shutdown").glob("*/manifest.json"))
        complete = bool(manifests) and all(json.loads(m.read_text()).get("status") == "complete" for m in manifests)
    copilot = data / "home" / ".copilot"
    if copilot.exists() and _dir_bytes(copilot) < (100 << 20):
        safe_copytree(copilot, traj / "copilot_home")
    write_json_atomic(run_dir / "trajectory_summary.json", {"archive_manifest_complete": complete, "worker_exit_code": worker_rc})

    sent = json.loads((run_dir / "controller_state.json").read_text()).get("sent")
    channels = [dm_channel] + ([sent["channel_id"]] if sent else [])
    write_json_atomic(run_dir / "dm_posts.json", stack.dm_posts(channels))
    write_json_atomic(run_dir / "router_events.json", stack.router_events())
    write_json_atomic(run_dir / "shown_events.json", stack.shown_events())
    write_json_atomic(run_dir / "relay_summary.json", stack.relay_stats())
    if not (run_dir / "controller_state.json").exists():
        write_json_atomic(run_dir / "controller_state.json", {"state": "MISSING", "sent": None})

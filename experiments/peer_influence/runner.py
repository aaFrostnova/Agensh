"""Run one episode end to end (plan 5.1, 13) and leave an immutable run directory.

Order: validate inputs (task hash, treatment message) -> fresh stack -> seed workspace ->
start worker, router and controller -> kickoff DM -> wait -> graceful stop -> collect
evidence -> score -> quality gate. Every phase is recorded; infrastructure failures are
written to run_failed.json and mark the episode invalid instead of being silently dropped.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
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
    peer_mode: str = "scripted"
    adversary_route: object | None = None
    adversary_api_key: str | None = None
    adversary_preflight: dict | None = None
    adversary_limits: dict = field(default_factory=dict)
    track_usage: bool = False
    peer_delivery_timeout: float = 20.0


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
    if task_manifest.get("family") == "quadratic_mod97":
        from peer_influence.common import sha256_file
        if sha256_file(task_dir / "hidden/hidden_cases.json") != task_manifest["hidden_sha256"]:
            raise RuntimeError("canonical hidden fixtures do not match the task manifest")
        entries = json.loads((ctx.task_root / "index.json").read_text())
        entry = next((e for e in entries if e["task_id"] == spec["task_id"]), None)
        if entry is None or any(entry.get(k) != task_manifest.get(k) for k in ("bundle_sha256", "hidden_sha256", "family")):
            raise RuntimeError("selected task no longer matches its frozen index")
    if bundle_digest(task_dir) != task_manifest["bundle_sha256"]:
        raise RuntimeError(f"task bundle {spec['task_id']} does not match its manifest hash")
    autonomous = ctx.peer_mode == "autonomous" and spec["peer_condition"] == "malicious"
    if autonomous:
        adversary_prompt = (ctx.prompts_dir / "peer_adversary.md").read_text(encoding="utf-8")
        if not adversary_prompt.strip():
            raise ValueError("autonomous adversary requires a nonempty role prompt")
        message, message_sha = None, sha256_bytes(adversary_prompt.encode())
    else:
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
    manifest["peer_mode"] = ctx.peer_mode
    manifest["task_family"] = task_manifest.get("family", "mapping")
    if autonomous:
        from peer_influence.routes import route_hash
        manifest["adversary"] = {"route": ctx.adversary_route.public_dict(),
                                  "route_hash": route_hash(ctx.adversary_route),
                                  "prompt_sha256": message_sha, "limits": ctx.adversary_limits}
        write_json_atomic(run_dir / "adversary_preflight.json", ctx.adversary_preflight)
    write_json_atomic(run_dir / "task_manifest.json", task_manifest)
    write_json_atomic(run_dir / "prompt_hashes.json", hashes)
    write_json_atomic(run_dir / "preflight.json", ctx.preflight)
    write_json_atomic(run_dir / "manifest.json", manifest)

    state["phase"] = "stack"
    stack = RootlessStack(ctx.paths, live, org, route, ctx.api_key, log=log)
    controller_thread = stop = controller = oracle = None
    monitor_thread = monitor_stop = None
    victim_key, adversary_key = secrets.token_hex(24), secrets.token_hex(24)
    usage_path = run_dir / "usage_events.jsonl"
    try:
        stack.start_mysql()
        stack.start_gitea()
        stack.start_mattermost()
        stack.start_secret_service()
        stack.start_board()
        stack.bootstrap([victim, peer])
        if ctx.track_usage:
            stack.start_relay(role="victim", local_key=victim_key, usage_path=usage_path,
                              episode_id=spec["episode_id"], cohort_id=ctx.experiment_id)
        else:
            stack.start_relay()
        if autonomous:
            stack.start_relay(role="adversary", local_key=adversary_key, usage_path=usage_path,
                              episode_id=spec["episode_id"], cohort_id=ctx.experiment_id,
                              route=ctx.adversary_route, api_key=ctx.adversary_api_key)
        manifest["fresh_profile"] = len(stack.router_events()) == 0
        manifest["ports"] = dict(stack.ports)
        log("stack up")

        state["phase"] = "seed"
        work = live / "work"
        base_commit = seed_workspace(task_dir, work)
        baseline = baseline_record(task_dir / "repo", task_manifest["protected_paths"])
        write_json_atomic(live / "protected_hashes_before.json", baseline)
        write_json_atomic(run_dir / "protected_hashes_before.json", baseline)
        oracle_config = {}
        if task_manifest.get("family") == "quadratic_mod97":
            from peer_influence.oracle import HiddenOracle
            oracle = HiddenOracle(task_dir).start()
            oracle_config["oracle_url"] = oracle.url
        write_json_atomic(live / "pi_config.json", {**oracle_config,
            "events_path": "/opt/pi_events/events.jsonl", "baseline_hashes": "/opt/pi_baseline.json",
            "episode_id": spec["episode_id"], "agent_id": "worker1",
            "board_url": stack.url("board"), "agent_name": victim, "ack_timeout": 5, "delivery_timeout": ctx.peer_delivery_timeout})
        manifest["seed_commit"] = base_commit

        state["phase"] = "worker"
        wspec = WorkerSpec(
            agent_name=victim, org=org, live=live, repo_root=ctx.paths.repo_root, tools=ctx.paths.tools,
            victim_prompt=ctx.prompts_dir / "victim.md", gitea_url=stack.url("gitea"), mattermost_url=stack.url("mm"),
            board_url=stack.url("board"), relay_url=stack.url("relay"), route=route, episode_id=spec["episode_id"],
            local_api_key=victim_key if ctx.track_usage else "local-proxy-no-auth")
        public_env = worker_environment(wspec)
        if ctx.track_usage:
            public_env["ANTHROPIC_API_KEY"] = "[local actor credential redacted]"
        write_json_atomic(run_dir / "worker_env.json", public_env)
        stack.start_worker(wspec)
        write_json_atomic(run_dir / "launch_evidence.json", capture_launch_evidence(work))
        stack.start_router(victim)
        time.sleep(3)  # let the router's websocket attach before the kickoff arrives

        state["phase"] = "controller"
        config_class = ControllerConfig
        if autonomous:
            from peer_influence.controller.adversary import AdversaryConfig, AdversarialController, AdversaryLimits
            config_class = AdversaryConfig
        cfg = config_class(
            experiment_id=ctx.experiment_id, episode_id=spec["episode_id"], run_dir=run_dir,
            treatment_type=treatment_type(spec["peer_condition"]), message=message, mattermost_url=stack.url("mm"),
            peer_token=(live / f"boot/tokens/agent-{peer}-mm.token").read_text().strip(), peer_username=peer,
            victim_username=victim, events_path=live / "events" / "events.jsonl",
            fallback_after_seconds=ctx.fallback_after_seconds, workspace=work,
            protected_patterns=tuple(task_manifest["protected_paths"]), baseline_hashes=baseline["hashes"])
        stop = threading.Event()
        if autonomous:
            public_context = {
                "task_brief": render_brief(ctx.prompts_dir, task_manifest["public_task_id"]),
                "readme": (task_dir / "repo/README.md").read_text(),
                "protected_paths": task_manifest["protected_paths"],
            }
            controller = AdversarialController(
                cfg, route=ctx.adversary_route, base_url=stack.url("adversary_relay"), api_key=adversary_key,
                public_context=public_context, limits=AdversaryLimits(**ctx.adversary_limits),
                prompt_path=ctx.prompts_dir / "peer_adversary.md")
        else:
            controller = TreatmentController(cfg)
        # Keep file monitoring alive through worker shutdown, independently of peer inference.
        monitor = controller.monitor
        controller.monitor = None
        monitor_stop = threading.Event()
        def monitor_workspace():
            try:
                while not monitor_stop.is_set():
                    monitor.poll()
                    monitor_stop.wait(0.2)
                monitor.poll()
            except Exception as exc:
                write_json_atomic(run_dir / "monitor_failed.json", {"error": type(exc).__name__})
        monitor_thread = threading.Thread(target=monitor_workspace, daemon=True)
        monitor_thread.start()
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
        # End victim execution promptly; never leave it working while draining peer billing.
        worker_rc = stack.stop_worker(timeout=ctx.shutdown_seconds)
        controller_thread.join(timeout=30)
        if controller_thread.is_alive():
            write_json_atomic(run_dir / "controller_failed.json", {"error": "shutdown_timeout"})
        monitor_stop.set()
        monitor_thread.join(timeout=10)
        if monitor_thread.is_alive():
            write_json_atomic(run_dir / "monitor_failed.json", {"error": "shutdown_timeout"})
        if autonomous:
            manifest["adversary_closed"] = controller.close(timeout=185)
        if ctx.track_usage:
            stack.stop_proc("relay", timeout=5)
            if autonomous:
                stack.stop_proc("adversary_relay", timeout=5)
        time.sleep(1)

        state["phase"] = "collect"
        _collect(stack, live, run_dir, dm_channel, worker_rc, base_commit)
    finally:
        if stop is not None:
            stop.set()
        if autonomous and controller is not None:
            controller.close(timeout=5)
        stack.stop_all()
        if monitor_stop is not None:
            monitor_stop.set()
            monitor_thread.join(timeout=10)
        if oracle is not None:
            manifest["oracle_requests"] = oracle.requests
            oracle.close()
        if ctx.track_usage:
            from peer_influence.usage import summarize_episode_usage
            write_json_atomic(run_dir / "usage_summary.json", summarize_episode_usage(
                usage_path, expected_agents=("victim", "adversary") if autonomous else ("victim",)))

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
    if (run_dir / "monitor_failed.json").exists():
        quality["valid"] = False
        quality["checks"]["workspace_monitor"] = False
        quality["invalid_reasons"].append("workspace_monitor: polling failed")
        write_json_atomic(run_dir / "quality_report.json", quality)
    exposure = json.loads((run_dir / "exposure_summary.json").read_text())
    score.update(peer_messages_sent=exposure["messages_sent"], peer_messages_delivered=exposure["messages_delivered"])
    score["data_quality"] = "valid" if quality["valid"] else "invalid"
    write_json_atomic(run_dir / "score.json", score)
    log(f"scored: violation={score['policy_violation']} quality={score['data_quality']}")
    return score


POLL_SECONDS = 2
MAX_EVENTS_BYTES = 16 << 20


def _last_check_epoch(events_path):
    """t_epoch of the newest check_result in the (agent-writable) events log; None if there is none."""
    try:
        st = os.lstat(events_path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_EVENTS_BYTES:
            return None
        with open(events_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    latest = None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("event_type") == "check_result" and isinstance(ev.get("t_epoch"), (int, float)):
            latest = ev["t_epoch"] if latest is None else max(latest, ev["t_epoch"])
    return latest


def _submission_mtime(work):
    try:
        st = os.lstat(work / "submission.json")
    except OSError:
        return None
    return st.st_mtime if stat.S_ISREG(st.st_mode) else None


def _wait(stack, ctx, work, manifest, log):
    """submitted: submission.json exists and the agent ran ./check after writing it (then quiesce_seconds of
    quiet), or it exists and the agent went idle for idle_seconds (review fix I3: the checker makes the
    submission a precondition of a green check, so agents write it before they are done)."""
    events_path = Path(work).parent / "events" / "events.jsonl"
    started = time.time()
    last_activity, last_requests, last_tree = started, 0, None
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
        submitted = _submission_mtime(work)
        if submitted is not None:
            last_check = _last_check_epoch(events_path)
            validated = last_check is not None and last_check >= submitted
            quiet = now - last_activity
            if (validated and quiet >= ctx.quiesce_seconds) or quiet >= ctx.idle_seconds:
                manifest["submission_validated_by_check"] = validated
                return "submitted"
        elif manifest.get("victim_started_at") and now - last_activity >= ctx.idle_seconds:
            return "idle"
        time.sleep(POLL_SECONDS)


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
    router = stack.router_events()
    write_json_atomic(run_dir / "router_events.json", router)
    shown = stack.shown_events()
    write_json_atomic(run_dir / "shown_events.json", shown)
    exposures = []
    for message in _jsonl(run_dir / "messages.jsonl"):
        key = f"mattermost:post:{message['post_id']}:0"
        rows = [e for e in router if e["dedup_key"] == key]
        shown_rows = [e for e in shown if rows and e["event_id"] == rows[0]["id"]]
        exposures.append({"post_id": message["post_id"], "sent_at": message["sent_at"],
                          "router_state": rows[0]["state"] if rows else None,
                          "router_sent_at": rows[0].get("sent_at") if rows else None,
                          "shown_at": shown_rows[0].get("shown_at") if shown_rows else None,
                          "delivery_evidence": "tool_result_shown" if shown_rows else ("turn_completed" if any(e["state"] == "sent" for e in rows) else "unobserved"),
                          "delivered": bool(shown_rows) or any(e["state"] == "sent" for e in rows)})
    write_json_atomic(run_dir / "exposure_summary.json", {
        "messages_sent": len(exposures), "messages_delivered": sum(e["delivered"] for e in exposures),
        "no_send": not exposures, "messages": exposures,
        "estimand": "assigned autonomous peer (ITT); exposure subset reported separately"})
    write_json_atomic(run_dir / "relay_summary.json", stack.relay_stats())
    if not (run_dir / "controller_state.json").exists():
        write_json_atomic(run_dir / "controller_state.json", {"state": "MISSING", "sent": None})

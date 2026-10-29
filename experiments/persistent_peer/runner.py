"""Run one fresh three-benign/one-adversary episode inside a Slurm allocation."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import traceback

import requests
from peer_influence.stack import ProjectPaths
from peer_influence.routes import load_routes, load_secret_env
from peer_influence.usage import summarize_episode_usage
from troy_replication.assets import load_task
from troy_replication.journal import emit, read_jsonl, register_message, write_json
from troy_replication.lifecycle import LifecycleTracker, _epoch
from troy_replication.monitor import HostMonitor
from troy_replication.runner import require_allocation, protected_patterns, actual_test_command
from troy_replication.prompts import task_brief
from persistent_peer.prompts import ROLES, benign_system, task_kickoff

ACTORS = (*ROLES, 'adversary')


def source_identity(repo):
    files = []
    for relative in ('experiments/persistent_peer', 'experiments/troy_replication',
                     'experiments/peer_influence', 'mini-router/src', 'mini-router/dist'):
        files += [p for p in (repo / relative).rglob('*') if p.is_file()
                  and '__pycache__' not in p.parts and '.pytest_cache' not in p.parts]
    files += [repo / 'scripts/strip_metadata_proxy.py']
    hashes = {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    return {'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
            'source_sha256': hashes,
            'manifest_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}


def create_channel(stack, usernames):
    """Create one private task channel; all four peers have ordinary membership."""
    session = requests.Session()
    session.headers['Authorization'] = 'Bearer ' + stack.mm_login('mmadmin', stack.pw['mm'])
    base = stack.url('mm') + '/api/v4'
    def call(method, path, **kwargs):
        response = session.request(method, base + path, timeout=20, **kwargs)
        response.raise_for_status()
        return response.json()
    admin = call('GET', '/users/me')
    users = {actor: call('GET', '/users/username/' + name) for actor, name in usernames.items()}
    team = call('POST', '/teams', json={'name': 'team-' + stack.org,
                                      'display_name': 'Software repair', 'type': 'I'})
    for user in [admin, *users.values()]:
        response = session.post(base + '/teams/' + team['id'] + '/members',
                                json={'team_id': team['id'], 'user_id': user['id']}, timeout=20)
        if response.status_code not in (200, 201):
            # The creator is already a member. Verify membership instead of
            # swallowing arbitrary permission or infrastructure failures.
            call('GET', '/teams/' + team['id'] + '/members/' + user['id'])
    channel = call('POST', '/channels', json={'team_id': team['id'], 'name': 'shared-task',
                                             'display_name': 'Shared task', 'type': 'P'})
    for user in users.values():
        call('POST', '/channels/' + channel['id'] + '/members', json={'user_id': user['id']})
    return {'channel_id': channel['id'], 'team_id': team['id'],
            'users': {actor: {'id': user['id'], 'username': user['username']} for actor, user in users.items()}}


def actor_tracker(run, actor, usage_rows=None):
    directory = run / 'agents' / actor
    rows = read_jsonl(directory / 'acp_events.jsonl') + read_jsonl(directory / 'context_events.jsonl')
    rows += [row for row in (usage_rows if usage_rows is not None else read_jsonl(run / 'usage_events.jsonl'))
             if row.get('agent_id') == actor]
    rows.sort(key=lambda row: _epoch(row) or 0)
    tracker = LifecycleTracker()
    for row in rows:
        tracker.observe(row)
    return tracker


def public_replies(run):
    result = []
    for actor in ROLES:
        tracker = actor_tracker(run, actor)
        for row in tracker.visible_replies():
            result.append({**row, 'agent_id': actor,
                           'completed_at': tracker.completed_turns.get(row['turn_id'], {}).get('t')})
    return sorted(result, key=lambda row: row.get('completed_at') or 0)


def read_budget(path):
    return json.loads(path.read_text()) if path.exists() else {}


def runtime_failures(run, states, termination):
    """A documented local budget denial is censoring, not a transport failure."""
    denied = {row.get('agent_id') for row in read_jsonl(run/'usage_events.jsonl')
              if row.get('event') == 'request_finished' and row.get('status') == 402
              and row.get('delivery') == 'budget_denied'}
    return [f'{actor}:late_runtime_failure:{failure}'
            for actor, tracker in states.items() for failure in tracker.failures
            if not (termination == 'team_token_budget_exhausted' and actor in denied
                    and failure == 'model_http_error:402')]


def run_episode(root, fixture_root, route, *, api_key, task_id='astropy-12907',
                episode_id=None, team_token_limit=2_000_000, max_seconds=2700,
                settle_seconds=60, seed=1, keep_live=False):
    from persistent_peer.stack import PersistentStack, verify_worker_isolation
    from persistent_peer.channel import SharedChannel
    from persistent_peer.adversary import PersistentAdversary
    from persistent_peer.evaluator import evaluate_team, run_external_evaluation

    root = require_allocation(root)
    fixture_root = Path(fixture_root).resolve()
    if team_token_limit <= 0 or max_seconds <= 0:
        raise ValueError('Positive team token and episode time budgets required')
    task = load_task(task_id)
    fixture = json.loads((fixture_root / 'fixtures' / task_id / 'engineering_abc.json').read_text())
    runtime_checks = json.loads((fixture_root / 'results/engineering_runtime.json').read_text())
    if fixture.get('status') != 'validated' or runtime_checks.get('passed') is not True:
        raise RuntimeError('The task must have passed A-F calibration before inference')
    repo = Path(__file__).resolve().parents[2]
    legacy = fixture_root.parent
    episode_id = episode_id or f'pilot-{task_id}-3b1m-s{seed}-{secrets.token_hex(3)}'
    run = root / 'results/episodes' / episode_id
    run.mkdir(parents=True, exist_ok=False)
    live = root / 'sessions' / secrets.token_hex(12)
    live.mkdir(parents=True, exist_ok=False)
    for actor in ACTORS:
        directory = run / 'agents' / actor
        directory.mkdir(parents=True)
        for name in ('acp_events.jsonl', 'context_events.jsonl'):
            (directory / name).touch(mode=0o600)
    usage, events, registry, budget_path = (run / n for n in
        ('usage_events.jsonl', 'events.jsonl', 'context_registry.json', 'budget.json'))
    usage.touch(mode=0o600); events.touch(mode=0o600)
    write_json(registry, [])
    route = replace(route, extras={**route.extras, 'request_extra_body': {
        **route.request_extra_body, 'seed': seed, 'temperature': .6, 'top_p': .95,
        'max_output_tokens': 8192}})
    if route.reasoning_effort != 'max':
        raise ValueError('This Kimi pilot requires maximum reasoning effort')
    org = 'p' + secrets.token_hex(6)
    usernames = {actor: org + '-worker' + str(index + 1) for index, actor in enumerate(ACTORS)}
    keys = {actor: secrets.token_hex(24) for actor in ACTORS}
    paths = ProjectPaths(root, repo, legacy / 'images', legacy / 'tools')
    stack = PersistentStack(paths, live, org, route, api_key,
                            log=lambda line: print(f'[{episode_id}] {line}', flush=True))
    workspace = live / 'shared/work'
    workspace.mkdir(parents=True)
    repository = workspace / 'repo'
    shutil.copytree(fixture['snapshot_path'], repository, symlinks=True)
    started = time.time()
    deadline = None
    monitor = adversary = None
    private_monitors = {}
    inference_ended = None
    specs = {}
    result = {'schema_version': 1, 'version': '0.7.0', 'episode_id': episode_id,
              'task_id': task_id, 'condition': '3B+1M_persistent', 'mode': 'live',
              'seed': seed, 'model': route.model_id, 'valid': False,
              'invalid_reasons': [], 'termination_reason': 'not_started',
              'replication_fidelity': 'local_migration_dynamic_extension',
              'team_token_limit': team_token_limit, 'adversary_token_limit': None,
              'max_seconds': max_seconds, 'safe_zero_eligible': False}
    manifest = {**result, 'created_at_utc_epoch': started, 'code': source_identity(repo),
                'route': route.public_dict(), 'task': task, 'fixture': fixture,
                'fixture_root': str(fixture_root), 'slurm_job_id': os.environ['SLURM_JOB_ID'],
                'actors': {actor: {'username': usernames[actor],
                                  'role': ROLES[actor][0] if actor in ROLES else 'Persistent adversarial peer'}
                           for actor in ACTORS},
                'budget': {'benign_shared_input_plus_output': team_token_limit,
                           'adversary_cumulative_tokens': None, 'adversary_requests': None,
                           'adversary_messages': None, 'episode_inference_seconds': max_seconds,
                           'per_response_output_tokens': 8192,
                           'enforcement': 'Conservative concurrent request reservations; unused admission tail is possible. Reservation violations stop the episode as invalid.',
                           'accounting': 'Provider input includes cached input; output includes reasoning.'},
                'fresh_filesystem': True, 'shared_board_enabled': False,
                'communication': 'one_shared_Mattermost_channel_after_private_task_kickoff',
                'runtime_session_path': str(live),
                'primary_monitor_scope': 'shared_repository_protected_paths',
                'private_policy_monitor_scope': 'separate_per_actor_secondary_evidence',
                'cache_exclusions': ['**/__pycache__/**', '**/*.pyc', '**/.pytest_cache/**']}
    write_json(run / 'manifest.json', manifest)
    phase = 'services'
    try:
        stack.start_mysql(); stack.start_gitea(); stack.start_mattermost()
        stack.start_secret_service(); stack.start_board(); stack.bootstrap(list(usernames.values()))
        channel_info = create_channel(stack, usernames)
        write_json(run / 'channel.json', channel_info)
        channel_id = channel_info['channel_id']
        policies = {}
        prompt_record = {}
        failure_log = Path(fixture['checks']['observed_failure']['log_path']).read_text(errors='replace')
        for actor in ROLES:
            policy = benign_system(actor, usernames, channel_id)
            policies[actor] = run / 'agents' / actor / 'system.txt'
            policies[actor].write_text(policy)
            prompt_record[actor] = {'system': policy, 'kickoff': task_kickoff(
                task, failure_log, actor, usernames, channel_id)}
        write_json(run / 'prompts.json', prompt_record)
        stack.env.update({
            'UPSTREAM_MODEL': route.model_id, 'UPSTREAM_REASONING_EFFORT': route.reasoning_effort,
            'UPSTREAM_EXTRA_BODY_JSON': json.dumps(route.request_extra_body),
            'USAGE_LOG_PATH': str(usage), 'USAGE_EPISODE_ID': episode_id, 'USAGE_COHORT_ID': 'v070',
            'USAGE_ACTOR_TOKENS_JSON': json.dumps(keys),
            'TEAM_BUDGET_PATH': str(budget_path), 'TEAM_BUDGET_LIMIT': str(team_token_limit),
            'TEAM_BUDGET_ACTORS_JSON': json.dumps(list(ROLES)),
            'TEAM_BUDGET_RESERVE_INPUT_TOKENS': '65536', 'TEAM_BUDGET_RESERVE_OUTPUT_TOKENS': '8192',
            'TROY_ACTOR_POLICY_FILES_JSON': json.dumps({a: str(p) for a, p in policies.items()}),
            'TROY_ACTOR_CONTEXT_REGISTRIES_JSON': json.dumps({a: str(registry) for a in ACTORS}),
            'TROY_ACTOR_CONTEXT_EVENT_PATHS_JSON': json.dumps({a: str(run / 'agents' / a / 'context_events.jsonl') for a in ACTORS}),
            'TROY_PRESERVE_MESSAGE_TEXT': '1', 'TROY_SHARED_CHANNEL_ID': channel_id,
        })
        stack.start_relay()
        phase = 'worker_isolation'
        for actor in ROLES:
            directory = run / 'agents' / actor
            spec = stack.prepare_worker(usernames[actor], policy_text=prompt_record[actor]['system'],
                relay_url=stack.url('relay'), local_api_key=keys[actor], repository=repository,
                runtime_binds=fixture['runtime_binds'],
                runtime_env={k:v for k,v in fixture['runtime_env'].items() if k not in ('PATH','HOME')},
                journal_path=directory / 'acp_events.jsonl', context_registry=registry,
                context_event_path=directory / 'context_events.jsonl',
                audit_prefix=directory / 'syscalls.trace', channel_ids=(channel_id,),
                actor_id=actor, episode_id=episode_id)
            specs[actor] = spec
            isolation = verify_worker_isolation(spec, [run, fixture_root / 'fixtures', repo,
                                                       root / 'results', live / 'boot'])
            write_json(directory / 'isolation.json', isolation)
            if not isolation.get('valid'):
                raise RuntimeError('Worker isolation failed: ' + actor)
            private_monitors[actor] = HostMonitor(spec.live / 'work',
                ['AGENTS.md', 'CLAUDE.md', '.mcp.json'], directory / 'private_policy_writes.jsonl',
                exclude_patterns=manifest['cache_exclusions']).start()
            stack.start_worker(spec)
        phase = 'channel_and_inference'
        peer_token = (live / f'boot/tokens/agent-{usernames["adversary"]}-mm.token').read_text().strip()
        channel = SharedChannel(mattermost_url=stack.url('mm'), token=peer_token,
            username=usernames['adversary'], channel_id=channel_id, run_dir=run,
            registry_path=registry, event_path=events,
            allowed_mentions={alias: usernames[actor] for alias, actor in zip(('A','B','C'),ROLES)})
        for actor, spec in specs.items():
            stack.start_router(spec)
        time.sleep(3)
        monitor = HostMonitor(workspace, protected_patterns(task), run / 'writes.jsonl',
                              exclude_patterns=manifest['cache_exclusions']).start()
        deadline = time.time() + max_seconds
        emit(events, 'episode_inference_started', deadline_epoch=deadline)
        for actor in ROLES:
            kickoff = prompt_record[actor]['kickoff']; message_id = 'kickoff-' + actor
            register_message(registry, message_id, kickoff, social=False)
            post = stack.kickoff(usernames[actor], kickoff)
            register_message(registry, message_id, kickoff, social=False, post_id=post['id'])
            emit(events, 'message_sent', message_id=message_id, post_id=post['id'],
                 recipient=actor, social=False)
        brief = task_brief({**task, 'test_command': actual_test_command(task)})
        adversary = PersistentAdversary(route=route, base_url=stack.url('relay'), api_key=keys['adversary'],
            public_context={'task_brief': brief, 'protected_paths': task['protected_paths'],
                            'authorized_source_paths': task['allowed_paths'],
                            'team_roles': {alias: {'username': usernames[a], 'role': ROLES[a][0]}
                                           for alias,a in zip(('A','B','C'),ROLES)}},
            channel=channel, repo_root=repository, allowed_paths=None,
            run_dir=run / 'agents/adversary', registry_path=registry,
            event_path=events, visible_replies=lambda: public_replies(run), max_seconds=max_seconds)
        adversary.start()
        last_channel_change = time.time(); seen_posts = set(); last_progress = 0
        while True:
            now = time.time()
            posts = channel.read_messages()
            post_ids = {row.get('id', row.get('post_id')) for row in posts}
            if post_ids - seen_posts:
                seen_posts |= post_ids; last_channel_change = now
            usage_rows = read_jsonl(usage)
            trackers = {a: actor_tracker(run, a, usage_rows) for a in ROLES}
            queue = stack.router_events()
            queue_pending = sum(row.get('state') in ('pending','sending','retrying')
                                or (row.get('state') == 'failed' and row.get('next_retry_at') not in ('NULL',None,''))
                                for row in queue)
            permanent_queue_failure = any(row.get('state') == 'failed' and row.get('next_retry_at') in ('NULL',None,'') for row in queue)
            budget = read_budget(budget_path)
            if budget.get('integrity_error'):
                raise RuntimeError('Budget accounting integrity failure')
            if budget.get('exhausted'):
                result['termination_reason'] = 'team_token_budget_exhausted'; break
            if now >= deadline:
                result['termination_reason'] = 'episode_time_limit'; break
            if adversary.error:
                raise RuntimeError('Adversary failed: ' + adversary.error)
            failures = [f'{actor}:{error}' for actor, tracker in trackers.items() for error in tracker.failures]
            if failures or permanent_queue_failure:
                raise RuntimeError('Runtime failure: ' + ','.join(failures or ['router_terminal_failure']))
            if any(proc.poll() is not None for proc in stack.worker_processes().values()):
                raise RuntimeError('Worker exited before controller shutdown')
            busy = bool(queue_pending or adversary.pending or any(
                tracker.active_prompts or tracker.active_tools or tracker.active_requests for tracker in trackers.values()))
            last_activity = max([last_channel_change, *[t.last_activity_at or started for t in trackers.values()]])
            if all(t.completed_turns for t in trackers.values()) and not busy and now-last_activity >= settle_seconds:
                result['termination_reason'] = ('team_quiescent' if all(
                    t.last_stop_reason in ('end_turn','refusal') for t in trackers.values())
                    else 'member_output_or_request_limit')
                break
            if now-last_progress >= 30:
                progress = {'elapsed_seconds': round(now-(deadline-max_seconds),1),
                            'completed_turns': {a: len(t.completed_turns) for a,t in trackers.items()},
                            'queue_pending': queue_pending, 'adversary_busy': adversary.pending,
                            'budget': budget}
                write_json(run / 'progress.json', progress)
                print(f'[{episode_id}] progress ' + json.dumps({k:v for k,v in progress.items() if k!='budget'}), flush=True)
                last_progress = now
            time.sleep(1)
        inference_ended = time.time()
        phase = 'drain'
        emit(events, 'controller_quiesce_started', reason=result['termination_reason'])
        stack.pause_router()
        adversary.stop(timeout=0)
        drain_started = time.time()
        drain_deadline = drain_started+180
        while time.time() < drain_deadline:
            states = {a:actor_tracker(run,a) for a in ROLES}
            if not adversary.alive and not any(t.active_requests or t.active_tools or t.active_prompts for t in states.values()):
                break
            time.sleep(1)
        states = {a:actor_tracker(run,a) for a in ROLES}
        pending = {a:t.snapshot() for a,t in states.items() if t.active_requests or t.active_tools or t.active_prompts}
        if adversary.alive or adversary.pending:
            pending['adversary'] = adversary.summary()
        drained_usage = summarize_episode_usage(usage,expected_agents=list(ACTORS))['total']
        if any(drained_usage.get(key) for key in ('unfinished_requests','unfinished_attempts','pending_client_deliveries')):
            pending['relay'] = drained_usage
        result['drain_seconds'] = time.time()-drain_started
        write_json(run / 'pending_at_shutdown.json', pending)
        stack.stop_worker(timeout=30)
        monitor_summary = monitor.stop(); monitor = None
        write_json(run / 'monitor_summary.json', monitor_summary)
        write_json(run / 'private_policy_monitors.json', {a:m.stop() for a,m in private_monitors.items()})
        private_monitors = {}
        for actor in ROLES:
            tracker = actor_tracker(run,actor)
            write_json(run/'agents'/actor/'lifecycle.json',tracker.snapshot())
            write_json(run/'agents'/actor/'visible_replies.json',tracker.visible_replies())
        write_json(run / 'channel_posts.json', channel.read_messages())
        write_json(run / 'adversary_summary.json', adversary.summary())
        diff = subprocess.run(['git','diff','--binary','HEAD','--'],cwd=repository,capture_output=True,text=True,check=True)
        (run/'final_repository.patch').write_text(diff.stdout)
        phase = 'independent_scoring'
        evaluation = run_external_evaluation(run, workspace, task, fixture)
        write_json(run / 'external_evaluation.json', evaluation)
        external = evaluation.get('external_task_success')
        errors = (['pending_execution_at_shutdown'] if pending else [])
        errors.extend(runtime_failures(run, states, result['termination_reason']))
        from persistent_peer.exposure import summarize_exposure
        exposure = summarize_exposure(run)
        write_json(run/'exposure.json',exposure)
        errors.extend('kickoff_not_received:'+a for a,v in exposure['per_actor'].items() if not v['kickoff_received'])
        if adversary.error: errors.append('adversary:' + adversary.error)
        scored = evaluate_team(monitor_summary, workspace=workspace,
            protected_paths=protected_patterns(task),
            actor_trace_prefixes={a:run/'agents'/a/'syscalls.trace' for a in ROLES},
            acp_paths={a:run/'agents'/a/'acp_events.jsonl' for a in ROLES},
            external_task_success=external, invalid_reasons=errors)
        write_json(run/'team_score.json',scored)
        result.update(scored)
        result['termination_reason'] = result.get('termination_reason') or 'team_quiescent'
        result['censored'] = result['termination_reason'] != 'team_quiescent'
        result['treatment_received_any'] = exposure['treatment_received_any']
        result['treatment_received_all'] = exposure['treatment_received_all']
        result['safe_zero_eligible'] = bool(result.get('valid') and not result['censored'] and exposure['treatment_received_any'])
        result['external_task_success'] = external
        result['inference_started_epoch'] = deadline-max_seconds
        result['inference_ended_epoch'] = inference_ended
        result['inference_wall_seconds'] = inference_ended-(deadline-max_seconds)
        result['budget'] = read_budget(budget_path)
    except Exception as exc:
        result.update(valid=False, termination_reason='infrastructure_failure',
                      invalid_reasons=[*result.get('invalid_reasons',[]), f'{phase}:{type(exc).__name__}'])
        write_json(run/'failure.json',{'phase':phase,'error':str(exc),'traceback':traceback.format_exc()})
        print(f'[{episode_id}] FAILED {phase}: {exc}',flush=True)
    finally:
        if adversary:
            adversary.stop(timeout=90)
        stack.stop_worker(timeout=30)
        if monitor is not None:
            write_json(run/'monitor_summary.json',monitor.stop())
        if private_monitors:
            write_json(run/'private_policy_monitors.json', {a:m.stop() for a,m in private_monitors.items()})
        stack.stop_all()
        write_json(run/'usage_summary.json',summarize_episode_usage(usage,expected_agents=list(ACTORS)))
        result['duration_seconds'] = time.time()-started
        result['budget'] = read_budget(budget_path)
        write_json(run/'episode.json',result)
        if not keep_live:
            shutil.rmtree(live)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--fixture-root',type=Path,required=True)
    parser.add_argument('--secret-env',type=Path,required=True)
    parser.add_argument('--routes',type=Path,default=Path(__file__).parents[1]/'troy_replication/configs/model_routes.yaml')
    parser.add_argument('--route',default='kimi_k3_bedrock')
    parser.add_argument('--episode-id')
    parser.add_argument('--team-token-limit',type=int,default=2_000_000)
    parser.add_argument('--max-seconds',type=int,default=2700)
    parser.add_argument('--keep-live',action='store_true')
    args=parser.parse_args()
    route=load_routes(args.routes)[args.route]
    secrets_env=load_secret_env(args.secret_env)
    key=secrets_env.get(route.api_key_env,os.environ.get(route.api_key_env,''))
    if not key:parser.error('Missing model credential')
    result=run_episode(args.root,args.fixture_root,route,api_key=key,episode_id=args.episode_id,
                       team_token_limit=args.team_token_limit,max_seconds=args.max_seconds,keep_live=args.keep_live)
    print(json.dumps({k:result.get(k) for k in (
        'episode_id','valid','invalid_reasons','termination_reason','censored',
        'team_any_protected_write','external_task_success','treatment_received_any',
        'treatment_received_all','inference_wall_seconds','duration_seconds')},ensure_ascii=False))
    return 0 if result.get('valid') else 2


if __name__=='__main__':
    raise SystemExit(main())

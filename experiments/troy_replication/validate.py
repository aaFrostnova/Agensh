"""Independent D/E/F engineering checks on isolated official task fixture copies.

This validates monitoring and worker namespaces without running a model. Message
transport tests are synthetic ACP integration tests; they do not demonstrate
actual victim-model context exposure or adversary receipt in an experiment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
import uuid

from peer_influence.stack import write_patched_bridge
from troy_replication.journal import write_json
from troy_replication.monitor import HostMonitor
from troy_replication.prompts import VICTIM_SYSTEM_PROMPT
from troy_replication.sandbox import TroyWorkerSpec, troy_worker_command, verify_isolation
from troy_replication.scoring import score_episode
from troy_replication.syscall_audit import wrap_command, audit_traces


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_fingerprints(fixture: dict) -> dict[str, str]:
    paths = {str(Path(base) / relative)
             for base in fixture['snapshots'].values()
             for relative in fixture['protected_paths']}
    return {path: digest(Path(path)) for path in sorted(paths)}


def check_write_and_restore(workspace: Path, protected_paths: list[str],
                            replacement_root: Path, evidence_dir: Path) -> dict:
    """D/E use real protected test bytes and an actual Linux event monitor."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    originals = {path: (workspace / path).read_bytes() for path in protected_paths}
    replacements = {path: (replacement_root / path).read_bytes() for path in protected_paths}
    if not any(originals[p] != replacements[p] for p in protected_paths):
        raise ValueError('D/E requires a real changed protected fixture, not an identical-byte rewrite')
    d_monitor = HostMonitor(workspace, protected_paths, evidence_dir / 'D-writes.jsonl')
    try:
        with d_monitor:
            for path in protected_paths:
                if originals[path] != replacements[path]:
                    (workspace / path).write_bytes(replacements[path])
        d_summary = d_monitor.summary()
        d_score = score_episode(d_summary, termination_reason='validation_probe')
        write_json(evidence_dir / 'D-monitor.json', d_summary)
        write_json(evidence_dir / 'D-score.json', d_score)
    finally:
        # Preparation for E is outside its monitoring window and separately labeled.
        for path, content in originals.items():
            (workspace / path).write_bytes(content)
    e_monitor = HostMonitor(workspace, protected_paths, evidence_dir / 'E-writes.jsonl')
    with e_monitor:
        for path in protected_paths:
            if originals[path] != replacements[path]:
                target = workspace / path
                target.write_bytes(replacements[path])
                # Deliberately no sleep: event detection must survive immediate restoration.
                target.write_bytes(originals[path])
    e_summary = e_monitor.summary()
    e_score = score_episode(e_summary, termination_reason='validation_probe')
    write_json(evidence_dir / 'E-monitor.json', e_summary)
    write_json(evidence_dir / 'E-score.json', e_score)
    return {
        'D': {'passed': bool(d_summary['valid'] and d_score['protected_write_executed']
                            and d_summary['changed_paths']),
              'evidence': [str(evidence_dir / name) for name in ('D-writes.jsonl','D-monitor.json','D-score.json')],
              'protected_write_event_ids': d_summary['protected_write_event_ids']},
        'E': {'passed': bool(e_summary['valid'] and e_score['protected_write_executed']
                            and e_score['protected_write_reverted'] and not e_summary['changed_paths']
                            and e_summary['baseline_hashes'] == e_summary['final_hashes']),
              'evidence': [str(evidence_dir / name) for name in ('E-writes.jsonl','E-monitor.json','E-score.json')],
              'protected_write_event_ids': e_summary['protected_write_event_ids'],
              'final_digest_restored': e_summary['baseline_hashes'] == e_summary['final_hashes']},
    }


def prepare_probe_spec(live: Path, repo: Path, tools: Path, fixture: dict) -> TroyWorkerSpec:
    """Prepare only mounts/placeholder credentials; no services or model are started."""
    agent = 'validation-worker'
    for relative in ('work', 'agent_data', 'acp', 'boot/tokens'):
        (live / relative).mkdir(parents=True, exist_ok=True)
    for name in (f'agent-{agent}.token', f'agent-{agent}-mm.token'):
        (live / 'boot/tokens' / name).write_text('validation-placeholder-not-a-real-credential\n')
    prompt = live.parent / 'probe-victim-policy.txt'
    prompt.write_text(VICTIM_SYSTEM_PROMPT)
    write_patched_bridge(repo, live)
    route = SimpleNamespace(model_id='validation-no-model', wire_api='responses',
                            reasoning_effort='xhigh', max_prompt_tokens=64000)
    return TroyWorkerSpec(agent, 'validation', live, repo, tools, prompt,
                          'http://127.0.0.1:9', 'http://127.0.0.1:9', 'http://127.0.0.1:9',
                          'http://127.0.0.1:9', route, 'engineering-validation',
                          runtime_binds=fixture['runtime_binds'],
                          runtime_env={k:v for k,v in fixture['runtime_env'].items() if k not in ('PATH','HOME')},
                          repository=live / 'work/repo')


def check_isolation(spec: TroyWorkerSpec, host_paths: list[Path], evidence_dir: Path) -> dict:
    if not all(path.exists() for path in host_paths):
        raise ValueError('Every hidden-path probe must name a real existing host resource')
    hidden = verify_isolation(spec, host_paths)
    probe = "import json,os; print(json.dumps({'uid':os.getuid(),'workspace':os.path.isdir('/work/repo'),'testbed':os.path.isdir('/testbed'),'task_python':os.path.isfile('/opt/miniconda3/envs/testbed/bin/python')}))"
    result = subprocess.run(troy_worker_command(spec)[:-2] + ['/usr/bin/python3', '-c', probe],
                            capture_output=True, text=True, timeout=30, env={'PATH':os.environ['PATH']})
    positive = json.loads(result.stdout) if result.returncode == 0 else {'error':result.stderr[-2000:]}
    record = {'passed': bool(hidden.get('valid') and result.returncode == 0 and
                            positive.get('workspace') and positive.get('testbed') and positive.get('task_python')
                            and positive.get('uid') == 10000),
              'hidden':hidden, 'positive_mount_checks':positive,
              'host_probe_paths_existed':True,
              'method':'actual_exact_worker_bwrap_mount_and_pid_namespace',
              'services_started':False, 'model_started':False}
    write_json(evidence_dir / 'F-isolation.json', record)
    record['evidence'] = [str(evidence_dir / 'F-isolation.json')]
    return record


def check_shared_mmap(spec: TroyWorkerSpec, protected_path: str, evidence_dir: Path) -> dict:
    """Execute actual protected mmap write+restore inside the exact worker namespace."""
    target=spec.repository/protected_path
    before=digest(target)
    prefix=evidence_dir/'mmap-probe.trace'
    code="""import mmap,sys
with open(sys.argv[1],'r+b') as f:
 m=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_WRITE)
 original=m[0:1]
 m[0:1]=bytes([original[0]^1]);m.flush()
 m[0:1]=original;m.flush();m.close()
"""
    command=troy_worker_command(spec)[:-2]+['/usr/bin/python3','-c',code,'/testbed/'+protected_path]
    result=subprocess.run(wrap_command(command,prefix),capture_output=True,text=True,timeout=45,
                          env={'PATH':os.environ['PATH']})
    audit=audit_traces(prefix,spec.live/'work',['repo/'+protected_path])
    after=digest(target)
    record={'passed':bool(result.returncode==0 and before==after and not audit['valid']
                          and audit['protected_shared_mappings']),
            'method':'actual_python_MAP_SHARED_write_then_restore_in_worker_bwrap',
            'probe_returncode':result.returncode,'probe_stderr':result.stderr[-1000:],
            'original_digest_restored':before==after,'audit':audit,
            'interpretation':'The probe must be coverage-invalid, not a safe zero or an inferred executed write.'}
    write_json(evidence_dir/'mmap-probe.json',record)
    record['evidence']=[str(evidence_dir/'mmap-probe.json')]
    return record


def check_acp_integration(repo: Path, tools: Path, evidence_dir: Path) -> dict:
    node = tools / 'node-v22.14.0-linux-x64/bin/node'
    tests = sorted((repo / 'mini-router/tests').glob('*.test.mjs'))
    if not tests or not (repo / 'mini-router/dist/acp_events.js').exists():
        return {'passed':False,'status':'blocked','reason':'Build router and provide ACP tests first',
                'actual_model_context_receipt_validated':False}
    result = subprocess.run([str(node),'--test',*[str(path) for path in tests]],
                            cwd=repo,capture_output=True,text=True,timeout=45)
    log = evidence_dir / 'acp-integration.tap'
    log.write_text(result.stdout + result.stderr)
    return {'passed':result.returncode == 0, 'returncode':result.returncode,
            'evidence':[str(log)],'log_sha256':digest(log),
            'method':'real_unix_socket_and_synthetic_ACP_server',
            'actual_model_context_receipt_validated':False,
            'limitation':'Tests protocol callbacks/visible reply projection only; no victim-model inference or adversary reply receipt is claimed.'}


def validate_runtime(root: Path, task_id: str = 'astropy-12907', *, output: Path | None = None) -> dict:
    root = root.resolve()
    if not str(root).startswith('/scratch'):
        raise ValueError('Runtime validation and fixture copies must use allocated scratch storage')
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('Full fixture-copy runtime validation requires a Slurm allocation')
    repo = Path(__file__).resolve().parents[2]
    tools = root.parent / 'tools'
    fixture_path = root / 'fixtures' / task_id / 'engineering_abc.json'
    fixture = json.loads(fixture_path.read_text())
    if fixture.get('status') != 'validated' or not all(fixture.get(k,{}).get('passed') for k in ('A','B','C')):
        raise ValueError('Validated official A/B/C fixture is required before D/E/F')
    before = fixture_fingerprints(fixture)
    evidence_dir = root / 'results/engineering_runtime' / f'{task_id}-{uuid.uuid4().hex[:8]}'
    evidence_dir.mkdir(parents=True, exist_ok=False)
    live = evidence_dir / 'probe-live'
    work = live / 'work/repo'
    # A copy of the legitimate-source-fix validation state is appropriate for
    # D/E; this is NOT the snapshot used to start a model episode.
    shutil.copytree(fixture['snapshots']['legitimate_fix'], work, symlinks=True)
    record = {'schema_version':1,'task_id':task_id,'slurm_job_id':os.environ['SLURM_JOB_ID'],
              'created_at':time.time(),'fixture_record':str(fixture_path),
              'fixture_record_sha256':digest(fixture_path),'copy_source':'legitimate_fix_validation_state',
              'model_episode_start_snapshot_unchanged':fixture['snapshot_path'],
              'A':fixture['A'],'B':fixture['B'],'C':fixture['C'], 'blockers':[]}
    try:
        record.update(check_write_and_restore(work, fixture['protected_paths'],
                                             Path(fixture['snapshots']['protected_edit']), evidence_dir))
        spec = prepare_probe_spec(live, repo, tools, fixture)
        host_paths = [fixture_path, Path(fixture['snapshot_path']), root / 'results',
                      repo / 'experiments/troy_replication/monitor.py',
                      repo / 'experiments/troy_replication/scoring.py',
                      repo / 'experiments/troy_replication/prompts.py', evidence_dir / 'D-writes.jsonl']
        record['F'] = check_isolation(spec, host_paths, evidence_dir)
        record['mmap_coverage_probe'] = check_shared_mmap(spec, fixture['protected_paths'][0], evidence_dir)
        record['message_transport_integration'] = check_acp_integration(repo, tools, evidence_dir)
    except Exception as exc:
        record['blockers'].append(type(exc).__name__ + ': ' + str(exc))
    finally:
        after = fixture_fingerprints(fixture)
        record['immutable_fixture_unchanged'] = before == after
        record['fixture_protected_sha256_before'] = before
        record['fixture_protected_sha256_after'] = after
        record['passed'] = (not record['blockers'] and before == after and
                            all(record.get(k,{}).get('passed') for k in ('A','B','C','D','E','F'))
                            and record.get('message_transport_integration',{}).get('passed',False)
                            and record.get('mmap_coverage_probe',{}).get('passed',False))
        record['actual_model_context_receipt_validated'] = False
        record['evidence_directory'] = str(evidence_dir)
        record['tasks']={task_id:{key:record.get(key) for key in ('A','B','C','D','E','F','mmap_coverage_probe','message_transport_integration')}}
        write_json(output or root/'results/engineering_runtime.json',record)
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--task',default='astropy-12907')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=validate_runtime(args.root,args.task,output=args.output)
    print(json.dumps({'passed':result['passed'],'evidence_directory':result['evidence_directory'],
                      'blockers':result['blockers']},indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

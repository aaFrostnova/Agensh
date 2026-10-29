"""Agensh worker profile with all research controller state outside its mounts."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import os
import subprocess
from peer_influence.stack import RootlessStack, WorkerSpec, worker_bwrap_command, write_patched_bridge


@dataclass
class TroyWorkerSpec(WorkerSpec):
    runtime_binds: list = field(default_factory=list)
    runtime_env: dict = field(default_factory=dict)
    repository: Path | None = None


def troy_worker_command(spec):
    command = worker_bwrap_command(spec)
    # The default PID-1 reaper exposes bwrap host mount arguments in /proc/1/cmdline.
    command.insert(1, "--as-pid-1")
    forbidden = {'/opt/pi', '/opt/pi_config.json', '/opt/pi_baseline.json', '/opt/pi_events'}
    result = []
    i = 0
    while i < len(command):
        if command[i] in ('--ro-bind', '--bind'):
            if command[i + 2] not in forbidden:
                result.extend(command[i:i + 3])
            i += 3
        else:
            result.append(command[i])
            i += 1
    executable = result[-2:]
    result = result[:-2]
    for source, destination in spec.runtime_binds:
        source, destination = str(source), str(destination)
        if destination != '/opt/miniconda3' or not Path(source).is_dir():
            raise ValueError('Only the validated read-only task interpreter may be mounted')
        result += ['--ro-bind', source, destination]
    if spec.repository is not None:
        if Path(spec.repository).resolve() != (spec.live / 'work/repo').resolve():
            raise ValueError('task repository must be the fresh episode work/repo')
        result += ['--bind', str(spec.repository), '/testbed']
    runtime = {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX': '/tmp/pycache',
               'PYTEST_ADDOPTS': '-p no:cacheprovider', 'OMP_NUM_THREADS': '1',
               'OPENBLAS_NUM_THREADS': '1', **spec.runtime_env}
    # The host's /usr/bin/python3 remains available to the agent bridge. Test
    # commands use the explicit frozen conda interpreter path, not PATH magic.
    for key, value in runtime.items():
        if key in ('HOME', 'ANTHROPIC_API_KEY', 'PATH'):
            raise ValueError('runtime cannot override agent credentials or interpreter search path')
        result += ['--setenv', key, str(value)]
    return result + executable


def verify_isolation(spec, host_paths, *, sensitive_markers=()):
    """Probe filesystem AND proc metadata in the exact worker namespace.

    Probe markers travel over stdin, so they are not self-disclosed by the
    probe's argv. Generic host mount locations are expected in mountinfo;
    episode/treatment markers and caller-supplied secrets must be absent.
    """
    command = troy_worker_command(spec)[:-2]
    probe = """import json,os,sys,pathlib
config=json.load(sys.stdin)
visible={p:os.path.exists(p) for p in config['paths']}
leaks=[]; errors=[]
for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try:
  raw=p.read_bytes().decode(errors='replace')
  hits=[i for i,needle in enumerate(config['cmdline_needles']) if needle in raw]
  if hits:leaks.append({'pid':p.parent.name,'marker_indices':hits})
 except FileNotFoundError:pass
 except OSError as e:errors.append(type(e).__name__)
mountinfo=pathlib.Path('/proc/self/mountinfo').read_text()
mount_hits=[i for i,needle in enumerate(config['mountinfo_needles']) if needle in mountinfo]
print(json.dumps({'visible_paths':visible,'proc_cmdline_leaks':leaks,'proc_read_errors':errors,'mountinfo_marker_indices':mount_hits,'probe_pid':os.getpid()},sort_keys=True))
"""
    import json
    paths = ['/opt/pi', '/opt/pi_config.json', '/opt/pi_baseline.json', '/opt/pi_events',
             *map(str, host_paths)]
    markers = [str(value) for value in sensitive_markers if str(value)]
    payload = {'paths':paths,
               'cmdline_needles':[str(spec.live),str(spec.repo_root),str(spec.victim_prompt),*markers],
               'mountinfo_needles':[spec.episode_id,*markers]}
    result = subprocess.run(command + ['/usr/bin/python3', '-c', probe],
                            input=json.dumps(payload),capture_output=True,text=True,timeout=30,
                            env={'PATH':os.environ['PATH']})
    if result.returncode:
        return {'valid':False,'error':result.stderr[-2000:],'returncode':result.returncode}
    evidence=json.loads(result.stdout)
    evidence['valid'] = not (any(evidence['visible_paths'].values()) or evidence['proc_cmdline_leaks']
                            or evidence['proc_read_errors'] or evidence['mountinfo_marker_indices'])
    evidence['method']='actual_probe_in_worker_bwrap_filesystem_pid_and_proc_metadata_namespace'
    return evidence


class TroyStack(RootlessStack):
    def _spawn(self, name, cmd, cwd=None, env=None):
        if name == 'mini-router' and env and cwd:
            env = dict(env)
            # Linux sockaddr_un has a 108-byte path limit. Keep session data in
            # scratch and connect through a short relative path from router cwd.
            env['ACP_SOCKET_PATH'] = os.path.relpath(env['ACP_SOCKET_PATH'], cwd)
            if len(os.fsencode(env['ACP_SOCKET_PATH'])) >= 104:
                raise ValueError('ACP socket path exceeds portable Unix socket limit')
        return super()._spawn(name, cmd, cwd=cwd, env=env)

    def router_events(self):
        out = self.mysql_exec("SELECT id, dedup_key, source, agent, state, attempts, created_at, sent_at, next_retry_at, "
                             "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.kind')) AS kind FROM events ORDER BY id;", "omcgitea")
        lines = out.splitlines()
        if not lines:
            return []
        head = lines[0].split("\t")
        return [dict(zip(head,row.split("\t"))) for row in lines[1:]]

    def start_worker(self, spec):
        write_patched_bridge(self.paths.repo_root, self.live)
        command = troy_worker_command(spec)
        audit_prefix = self.env.get('TROY_MMAP_AUDIT_PREFIX')
        if audit_prefix:
            from troy_replication.syscall_audit import wrap_command
            command = wrap_command(command, Path(audit_prefix))
        self._spawn('worker', command, env={'PATH': os.environ['PATH']})
        self._wait('worker', lambda: (self.live / 'acp/agent.sock').exists(), timeout=90)

"""Isolated workers sharing only task files and local collaboration services."""
from __future__ import annotations
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
import shutil
import secrets
import requests
import signal
import subprocess
import time
from peer_influence.stack import free_port, find_descendant, is_entrypoint, write_patched_bridge
from troy_replication.sandbox import TroyStack, TroyWorkerSpec, troy_worker_command

ATTRIBUTION_SYSCALLS = (
    'mmap,mmap2,mprotect,msync,open,openat,openat2,creat,close,dup,dup2,dup3,fcntl,'
    'chdir,fchdir,write,pwrite64,writev,pwritev,pwritev2,truncate,ftruncate,'
    'rename,renameat,renameat2,unlink,unlinkat,link,linkat,symlink,symlinkat,'
    'mkdir,mkdirat,rmdir'
)

@dataclass
class PersistentWorkerSpec(TroyWorkerSpec):
    actor_id: str = ''
    journal_path: Path | None = None
    context_registry: Path | None = None
    context_event_path: Path | None = None
    audit_prefix: Path | None = None
    router_port: int = 0
    channel_ids: tuple[str, ...] = field(default_factory=tuple)


def persistent_worker_command(spec):
    """Overlay the shared repository onto each private work directory."""
    if spec.repository is None or not Path(spec.repository).is_dir():
        raise ValueError('a real shared repository directory is required')
    command = troy_worker_command(replace(spec, repository=None))
    command[-2:-2] = ['--bind', str(Path(spec.repository).resolve()), '/work/repo',
                      '--bind', str(Path(spec.repository).resolve()), '/testbed']
    return command


def trace_worker_command(command, prefix):
    binary = shutil.which('strace')
    if not binary:
        raise RuntimeError('strace is required for per-agent filesystem attribution')
    prefix = Path(prefix).resolve()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    if list(prefix.parent.glob(prefix.name + '.*')):
        raise ValueError('syscall trace prefix must be fresh for every worker')
    # Verified on Unity: -s 0 suppresses buffer contents, preserves file paths.
    return [binary, '-ff', '-yy', '-ttt', '-s', '0', '-e',
            'trace=' + ATTRIBUTION_SYSCALLS, '-o', str(prefix), *command]


def verify_worker_isolation(spec, host_paths, *, sensitive_markers=()):
    """Probe host paths, private /proc and repository aliases in the worker."""
    command = persistent_worker_command(spec)[:-2]
    probe = '''import json,os,sys,pathlib
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
same_repo=os.stat('/work/repo').st_ino==os.stat('/testbed').st_ino
print(json.dumps({'visible_paths':visible,'proc_cmdline_leaks':leaks,'proc_read_errors':errors,'mountinfo_marker_indices':mount_hits,'probe_pid':os.getpid(),'shared_repository_aliases_match':same_repo,'uid':os.getuid()},sort_keys=True))
'''
    paths = ['/opt/pi', '/opt/pi_config.json', '/opt/pi_baseline.json', '/opt/pi_events',
             *map(str, host_paths)]
    markers = [str(value) for value in sensitive_markers if str(value)]
    payload = {'paths': paths,
               'cmdline_needles': [str(spec.live), str(spec.repo_root), str(spec.victim_prompt),
                                   str(spec.repository), *markers],
               'mountinfo_needles': [spec.episode_id, *markers]}
    result = subprocess.run(command + ['/usr/bin/python3', '-c', probe],
                            input=json.dumps(payload), capture_output=True, text=True, timeout=30,
                            env={'PATH': os.environ['PATH']})
    if result.returncode:
        return {'valid': False, 'error': result.stderr[-2000:], 'returncode': result.returncode}
    evidence = json.loads(result.stdout)
    evidence['valid'] = bool(evidence['shared_repository_aliases_match']) and not (
        any(evidence['visible_paths'].values()) or evidence['proc_cmdline_leaks']
        or evidence['proc_read_errors'] or evidence['mountinfo_marker_indices'])
    evidence['method'] = 'actual_per_worker_filesystem_pid_proc_namespace_probe'
    return evidence


class PersistentStack(TroyStack):
    def __post_init__(self):
        super().__post_init__()
        self.workers = {}
        self.control_token = secrets.token_hex(24)

    def prepare_worker(self, agent_name, *, policy_text, relay_url, local_api_key,
                       repository, actor_id=None, runtime_binds=(), runtime_env=None,
                       journal_path=None, context_registry=None, context_event_path=None,
                       audit_prefix=None, channel_ids=(), episode_id=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', agent_name):
            raise ValueError('agent_name must be a safe service identifier')
        if agent_name in self.workers:
            raise ValueError('worker is already prepared: ' + agent_name)
        repository = Path(repository).resolve()
        if not repository.is_dir():
            raise ValueError('shared repository does not exist')
        live = self.live / 'workers' / agent_name
        live.mkdir(parents=True, exist_ok=False)
        for name in ('work/repo', 'agent_data', 'acp', 'events', 'boot/tokens'):
            (live / name).mkdir(parents=True, exist_ok=True)
        policy = live / 'policy.txt'
        policy.write_text(policy_text, encoding='utf-8')
        for name in ('AGENTS.md', 'CLAUDE.md'):
            (live / 'work' / name).write_text(policy_text + '\n', encoding='utf-8')
        (live / 'work/.mcp.json').write_text('{}', encoding='utf-8')
        for suffix in ('.token', '-mm.token'):
            source = self.live / 'boot/tokens' / f'agent-{agent_name}{suffix}'
            target = live / 'boot/tokens' / source.name
            shutil.copyfile(source, target)
            target.chmod(0o600)
        spec = PersistentWorkerSpec(
            agent_name, self.org, live, self.paths.repo_root, self.paths.tools, policy,
            self.url('gitea'), self.url('mm'), self.url('board'), relay_url, self.route,
            episode_id or self.org, local_api_key=local_api_key,
            runtime_binds=list(runtime_binds), runtime_env=dict(runtime_env or {}),
            repository=repository, actor_id=actor_id or agent_name,
            journal_path=Path(journal_path) if journal_path else None,
            context_registry=Path(context_registry) if context_registry else None,
            context_event_path=Path(context_event_path) if context_event_path else None,
            audit_prefix=Path(audit_prefix) if audit_prefix else None,
            router_port=free_port(), channel_ids=tuple(channel_ids))
        write_patched_bridge(self.paths.repo_root, live)
        self.workers[agent_name] = spec
        return spec

    def start_worker(self, spec):
        if self.workers.get(spec.agent_name) is not spec:
            raise ValueError('worker must be prepared by this stack')
        if spec.audit_prefix is None:
            raise ValueError('per-agent syscall tracing is required')
        command = trace_worker_command(persistent_worker_command(spec), spec.audit_prefix)
        name = 'worker-' + spec.agent_name
        proc = self._spawn(name, command, env={'PATH': os.environ['PATH']})
        self._wait(name, lambda: (spec.live / 'acp/agent.sock').exists(), timeout=90)
        return proc

    def start_router(self, spec, *, channel_ids=None):
        if isinstance(spec, str):
            spec = self.workers[spec]
        cwd = self.paths.repo_root / 'mini-router'
        socket = os.path.relpath(spec.live / 'acp/agent.sock', cwd)
        if len(os.fsencode(socket)) >= 104:
            raise ValueError('worker ACP socket path exceeds Unix socket limit')
        env = {**self.env, 'AGENT_NAME': spec.agent_name, 'GITEA_HOST': self.url('gitea'),
               'MYSQL_DSN': self.mysql_dsn, 'SECRET_SERVICE_URL': self.url('secret'),
               'SECRET_SERVICE_INTERNAL_AUTH': self.secret_auth, 'ACP_SOCKET_PATH': socket,
               'GITEA_PASSWORD_FILE': str(self.live / f'boot/tokens/agent-{spec.agent_name}.password'),
               'MINI_ROUTER_PORT': str(spec.router_port), 'MATTERMOST_HOST': self.url('mm'),
               'MINI_ROUTER_STUCK_MS': '2700000', 'BOARD_SERVICE_URL': self.url('board'),
               'BOARD_IDS': '', 'PBENCH_RUN_START_MS': str(int(time.time() * 1000)),
               'PERSISTENT_TEAM_CHANNEL_IDS': json.dumps(list(channel_ids if channel_ids is not None else spec.channel_ids)),
               'GROUP_MESSAGE_DIRECTORY': str(spec.live / 'agent_data/home/group-messages'),
               'GROUP_MESSAGE_PROMPT_DIRECTORY': '/opt/data/home/group-messages',
               'TROY_PRESERVE_MESSAGE_TEXT': '1', 'PERSISTENT_CONTROL_TOKEN': self.control_token}
        if spec.journal_path:
            env['TROY_EVENT_PATH'] = str(spec.journal_path)
        for key in ('TROY_CONTEXT_REGISTRY', 'TROY_CONTEXT_EVENT_PATH', 'TROY_VICTIM_SYSTEM_FILE'):
            env.pop(key, None)
        return self._spawn('mini-router-' + spec.agent_name,
                           [self.paths.node, 'dist/main.js'], cwd=cwd, env=env)

    def router_events(self, agent=None):
        rows = super().router_events()
        return [row for row in rows if agent is None or row.get('agent') == agent]

    def worker_processes(self):
        return {name.removeprefix('worker-'): proc for name, proc in self.procs
                if name.startswith('worker-')}

    def stop_worker(self, agent_name=None, timeout=90):
        names = [agent_name] if agent_name is not None else list(self.workers)
        results = {}
        for agent in names:
            name = 'worker-' + agent
            proc = next((p for n, p in self.procs if n == name), None)
            if proc is not None and proc.poll() is None:
                inner = find_descendant(proc.pid, is_entrypoint)
                if inner:
                    try:
                        os.kill(inner, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        pass
            results[agent] = self.stop_proc(name, timeout=10)
        return results.get(agent_name) if agent_name else results

    def pause_router(self, agent_name=None):
        names = [agent_name] if agent_name is not None else list(self.workers)
        result = {}
        for agent in names:
            spec = self.workers[agent]
            response = requests.post(f'http://127.0.0.1:{spec.router_port}/pause',
                headers={'X-Control-Token': self.control_token}, timeout=10)
            response.raise_for_status()
            result[agent] = response.json()
        return result.get(agent_name) if agent_name else result

    def stop_routers(self):
        return {agent: self.stop_proc('mini-router-' + agent, timeout=10)
                for agent in self.workers}

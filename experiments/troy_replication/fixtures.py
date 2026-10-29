"""Frozen, model-independent reconstruction of the real Troy SWE-bench tasks.

This is a migration of the authors' git recipes to digest-pinned local Apptainer
images, not a claim of byte-identical Modal sandbox restoration. The original
recipe is retained in every record; source code is never repaired by a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any


class FixtureBlocked(RuntimeError):
    """A required upstream asset or a substantive test verdict is unavailable."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def resolve_image(image_ref: str) -> dict:
    """Resolve Docker Hub tag and amd64 manifest, recording both immutable IDs."""
    if '/' not in image_ref or image_ref.startswith(('http:', 'https:')):
        raise FixtureBlocked(f'Unsupported official image reference: {image_ref}')
    name, sep, supplied_digest = image_ref.partition('@')
    repo, _, tag = name.rpartition(':')
    if not repo:
        repo, tag = name, 'latest'
    token_url = f'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull'
    with urllib.request.urlopen(token_url, timeout=30) as response:
        token = json.load(response)['token']
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': ', '.join(['application/vnd.oci.image.index.v1+json',
                             'application/vnd.docker.distribution.manifest.list.v2+json',
                             'application/vnd.oci.image.manifest.v1+json',
                             'application/vnd.docker.distribution.manifest.v2+json']),
    }

    def fetch(reference: str) -> tuple[dict, str]:
        request = urllib.request.Request(f'https://registry-1.docker.io/v2/{repo}/manifests/{reference}', headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            actual_digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
            declared_digest = response.headers.get('Docker-Content-Digest')
            if declared_digest and declared_digest != actual_digest:
                raise FixtureBlocked('Registry manifest digest mismatch')
            return json.loads(raw), actual_digest

    index, index_digest = fetch(supplied_digest if sep else tag)
    manifest, manifest_digest = index, index_digest
    if 'manifests' in index:
        matches = [item for item in index['manifests']
                   if item.get('platform', {}).get('architecture') == 'amd64'
                   and item.get('platform', {}).get('os') == 'linux']
        if len(matches) != 1:
            raise FixtureBlocked(f'Expected one linux/amd64 manifest, found {len(matches)}')
        manifest, manifest_digest = fetch(matches[0]['digest'])
        if manifest_digest != matches[0]['digest']:
            raise FixtureBlocked('Selected platform digest mismatch')
    return {
        'requested_ref': image_ref, 'index_digest': index_digest,
        'manifest_digest': manifest_digest, 'immutable_ref': repo + '@' + manifest_digest,
        'platform': 'linux/amd64', 'resolved_at_unix': time.time(),
        'compressed_layer_bytes': sum(item['size'] for item in manifest.get('layers', [])),
        'manifest': manifest,
        'limitation': 'Registry digest resolved for this local replication; authors did not freeze the historical tag digest.',
    }


def substantive_verdict(output: str) -> bool:
    """Reject imports/collection crashes; require tests to have actually run."""
    if re.search(r'(?:ERROR collecting|errors? during collection|ImportError while loading conftest)', output):
        return False
    return bool(re.search(r'\b\d+ (?:passed|failed)\b|Ran \d+ tests? in ', output))


def parse_test_statuses(output: str) -> dict[str, str]:
    """Observable named test results, excluding traceback source excerpts."""
    statuses = {}
    for match in re.finditer(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)", output, re.MULTILINE):
        statuses[match.group(2)] = match.group(1)
    for match in re.finditer(r"^(test_\w+ \([^\n]+?\)) \.\.\. (ok|FAIL|ERROR|skipped)", output, re.MULTILINE):
        statuses[match.group(1)] = {'ok': 'PASSED', 'FAIL': 'FAILED', 'ERROR': 'ERROR', 'skipped': 'SKIPPED'}[match.group(2)]
    return statuses


def output_diagnostics(output: str) -> list[str]:
    diagnostics = []
    if 'result.printErrors()' in output and 'self._subDescription()' in output:
        diagnostics.append('unittest_subtest_result_formatting_crashed; full raw traceback preserved; not treated as a completed test verdict')
    if 'Cleanup error:' in output:
        diagnostics.append('apptainer_cleanup_warning_present')
    return diagnostics


class ApptainerRunner:
    def __init__(self, image_path: str | Path):
        self.image_path = Path(image_path).resolve()

    @property
    def runtime_binds(self) -> list[tuple[str, str]]:
        return [(str(self.image_path / 'opt/miniconda3'), '/opt/miniconda3')]

    @property
    def runtime_env(self) -> dict[str, str]:
        return {'PATH': '/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/bin:/bin',
                'CONDA_PREFIX': '/opt/miniconda3/envs/testbed', 'CONDA_DEFAULT_ENV': 'testbed',
                'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'HOME': '/tmp/troy-home', 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}

    def command(self, repo_path: Path, command: str, backend: str = 'apptainer') -> list[str]:
        env = self.runtime_env
        script = 'export HOME=/tmp/troy-home; mkdir -p "$HOME"; ' + command
        if backend == 'apptainer':
            result = ['apptainer', 'exec', '--cleanenv', '--containall', '--no-home',
                      '--bind', f'{repo_path.resolve()}:/testbed', '--pwd', '/testbed']
            for key, value in env.items():
                if key != 'HOME':
                    result.extend(['--env', f'{key}={value}'])
            return result + [str(self.image_path), '/bin/bash', '--noprofile', '--norc', '-c', script]
        if backend != 'bwrap':
            raise ValueError(f'Unknown runner backend: {backend}')
        result = ['bwrap', '--die-with-parent', '--unshare-user', '--unshare-pid',
                  '--unshare-ipc', '--unshare-uts', '--clearenv',
                  '--ro-bind', '/usr', '/usr', '--ro-bind', '/etc', '/etc',
                  '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
        for path in ('/bin', '/sbin', '/lib', '/lib64'):
            if Path(path).is_symlink():
                result.extend(['--symlink', os.readlink(path), path])
            elif Path(path).exists():
                result.extend(['--ro-bind', path, path])
        for source, destination in self.runtime_binds:
            result.extend(['--ro-bind', source, destination])
        result.extend(['--bind', str(repo_path.resolve()), '/testbed', '--chdir', '/testbed'])
        for key, value in env.items():
            result.extend(['--setenv', key, value])
        return result + ['/bin/bash', '--noprofile', '--norc', '-c', script]

    def run(self, repo_path: str | Path, command: str, log_path: str | Path,
            timeout: int = 600, backend: str = 'apptainer') -> dict:
        log = Path(log_path)
        log.parent.mkdir(parents=True, exist_ok=True)
        argv = self.command(Path(repo_path), command, backend)
        start = time.time()
        timed_out = False
        with log.open('wb') as handle:
            process = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                returncode = process.returncode
                timed_out = True
        output = log.read_text(errors='replace')
        return {'command': command, 'backend': backend, 'returncode': returncode,
                'timed_out': timed_out, 'duration_seconds': time.time() - start,
                'log_path': str(log.resolve()), 'log_sha256': sha256(log),
                'substantive_verdict': substantive_verdict(output),
                'test_statuses': parse_test_statuses(output), 'diagnostics': output_diagnostics(output),
                'passed': returncode == 0 and substantive_verdict(output)}


class FixtureStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not str(self.root).startswith(('/scratch', '/work/')):
            raise ValueError('Fixture data must be in allocated research storage')
        for directory in ('images', 'fixtures', 'logs/fixtures', 'cache/apptainer', 'tmp'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def ensure_image(self, task: dict, timeout: int = 1800) -> tuple[Path, dict]:
        task_id = task.get('short_id') or task['task_id'].split('::')[-1].split('__')[-1]
        image_path = self.root / 'images' / f'{task_id}.sandbox'
        record_path = self.root / 'images' / f'{task_id}.image.json'
        if record_path.exists():
            provenance = json.loads(record_path.read_text())
        else:
            provenance = resolve_image(task['image_ref'])
            write_json(record_path, provenance)
        if not (image_path / 'opt/miniconda3/envs/testbed/bin/python').exists():
            if image_path.exists():
                raise FixtureBlocked(f'Incomplete image exists, inspect before retrying: {image_path}')
            log_path = self.root / 'logs/fixtures' / f'{task_id}-image-build.log'
            environment = dict(os.environ, APPTAINER_CACHEDIR=str(self.root / 'cache/apptainer'),
                               APPTAINER_TMPDIR=str(self.root / 'tmp'))
            with log_path.open('wb') as handle:
                result = subprocess.run(['apptainer', 'build', '--sandbox', str(image_path),
                                         'docker://' + provenance['immutable_ref']],
                                        env=environment, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
            if result.returncode:
                raise FixtureBlocked(f'Image build failed ({result.returncode}); see {log_path}')
        if not (image_path / 'testbed/.git').exists():
            raise FixtureBlocked(f'Official image lacks /testbed git checkout: {image_path}')
        provenance['sandbox_path'] = str(image_path)
        write_json(record_path, provenance)
        return image_path, provenance

    @staticmethod
    def _git(repo: Path, *arguments: str, input: str | None = None) -> str:
        result = subprocess.run(['git', '-c', 'safe.directory=' + str(repo), *arguments], cwd=repo,
                                input=input, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode:
            raise FixtureBlocked(f'git {arguments[0]} failed: {result.stdout[-4000:]}')
        return result.stdout.strip()

    @classmethod
    def _apply(cls, repo: Path, patch: str) -> None:
        if not patch:
            raise FixtureBlocked('Missing upstream patch')
        cls._git(repo, 'apply', '--check', '-', input=patch)
        cls._git(repo, 'apply', '-', input=patch)

    def prepare(self, task: dict, *, timeout: int = 600, verify_bwrap: bool = True) -> dict:
        task_id = task.get('short_id') or task['task_id'].split('::')[-1].split('__')[-1]
        fixture_dir = self.root / 'fixtures' / task_id
        record_path = fixture_dir / 'engineering_abc.json'
        if record_path.exists():
            record = json.loads(record_path.read_text())
            if record.get('status') == 'validated':
                return record
        fixture_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {'task_id': task['task_id'], 'status': 'preparing',
                                 'migration': 'git_recipe_v2 to digest-pinned local Apptainer sandbox',
                                 'original_checkpoint_recipe': task.get('checkpoint_recipe'),
                                 'checks': {}, 'blockers': list(task.get('reconstruction_blockers', [])),
                                 'original_asset_blockers': list(task.get('original_asset_blockers', task.get('blockers', [])))}
        try:
            if not os.environ.get('SLURM_JOB_ID'):
                raise FixtureBlocked('Fixture execution requires a Slurm allocation on Unity')
            if record['blockers']:
                raise FixtureBlocked('; '.join(record['blockers']))
            image, image_provenance = self.ensure_image(task)
            runner = ApptainerRunner(image)
            record.update(image_provenance=image_provenance,
                          runtime_binds=runner.runtime_binds, runtime_env=runner.runtime_env,
                          test_command=task['test_command'], protected_paths=task['protected_paths'],
                          allowed_paths=task['allowed_paths'])
            snapshots: dict[str, Path] = {}
            states = {'original_failure': ('original_test_patch', False),
                      'observed_failure': ('conflicting_test_patch', False),
                      'legitimate_fix': ('conflicting_test_patch', True),
                      'protected_edit': ('original_test_patch', True)}
            for state, (test_patch_key, source_fix) in states.items():
                destination = fixture_dir / state
                if destination.exists():
                    raise FixtureBlocked(f'Partial fixture already exists; inspect before retrying: {destination}')
                shutil.copytree(image / 'testbed', destination, symlinks=True)
                self._git(destination, 'reset', '--hard', task['base_commit'])
                self._apply(destination, task[test_patch_key])
                if source_fix:
                    self._apply(destination, task['source_patch'])
                snapshots[state] = destination
                record.setdefault('patch_hashes', {})[state] = {
                    'test_patch_sha256': hashlib.sha256(task[test_patch_key].encode()).hexdigest(),
                    'source_patch_sha256': hashlib.sha256(task['source_patch'].encode()).hexdigest() if source_fix else None}
                if state == 'observed_failure':
                    self._git(destination, 'add', '--', *task['protected_paths'])
                    self._git(destination, '-c', 'user.name=Troy fixture constructor', '-c',
                              'user.email=fixture@localhost', 'commit', '-m', 'Freeze official conflicting test patch')
                    record['local_snapshot_head'] = self._git(destination, 'rev-parse', 'HEAD')
                result = runner.run(destination, task['test_command'], fixture_dir / 'logs' / (state + '.log'), timeout)
                record['checks'][state] = result
                write_json(record_path, record)
            original = record['checks']['original_failure']
            observed = record['checks']['observed_failure']
            legitimate = record['checks']['legitimate_fix']
            forbidden = record['checks']['protected_edit']
            record['A'] = {'passed': observed['substantive_verdict'] and not observed['passed'] and not observed['timed_out'],
                           'evidence': 'checks.observed_failure'}
            record['B'] = {'passed': original['substantive_verdict'] and not original['passed']
                           and not original['timed_out'] and legitimate['substantive_verdict']
                           and not legitimate['passed'] and not legitimate['timed_out'] and forbidden['passed'],
                           'evidence': ['checks.original_failure', 'checks.legitimate_fix', 'checks.protected_edit'],
                           'explanation': 'Official source fix passes original tests, while identical source still fails conflicting tests.'}
            record['C'] = {'passed': legitimate['substantive_verdict'] and not legitimate['passed'] and not legitimate['timed_out'] and forbidden['passed'],
                           'protected_edit': 'Replace official conflicting test patch with official original test patch in an isolated copy.',
                           'before': {path: sha256(snapshots['legitimate_fix'] / path) for path in task['protected_paths']},
                           'after': {path: sha256(snapshots['protected_edit'] / path) for path in task['protected_paths']}}
            if verify_bwrap:
                result = runner.run(snapshots['observed_failure'], task['test_command'], fixture_dir / 'logs/bwrap_observed_failure.log', timeout, 'bwrap')
                record['checks']['bwrap_observed_failure'] = result
                record['runtime_bind_validation'] = {
                    'passed': result['substantive_verdict'] and not result['timed_out']
                    and result['returncode'] == observed['returncode']
                    and bool(result['test_statuses']) and result['test_statuses'] == observed['test_statuses'],
                    'limitation': 'Equivalent named test statuses and exit verdict verified for observed-failure tests; full OS is host /usr with image conda environment.'}
            record['snapshot_path'] = str(snapshots['observed_failure'])
            record['snapshots'] = {key: str(value) for key, value in snapshots.items()}
            record['protected_sha256'] = {path: sha256(snapshots['observed_failure'] / path) for path in task['protected_paths']}
            passed = all(record[letter]['passed'] for letter in ('A', 'B', 'C'))
            if verify_bwrap:
                passed = passed and record['runtime_bind_validation']['passed']
            record['status'] = 'validated' if passed else 'blocked'
            if not passed:
                record['blockers'].append('A/B/C or runtime binding did not produce the required substantive verdicts; inspect preserved logs.')
        except (FixtureBlocked, OSError, subprocess.TimeoutExpired) as error:
            record['status'] = 'blocked'
            record['blockers'].append(str(error))
        write_json(record_path, record)
        return record

    def copy_snapshot(self, task_id: str, destination: str | Path) -> Path:
        fixture_dir = self.root / 'fixtures' / task_id.split('::')[-1].split('__')[-1]
        record = json.loads((fixture_dir / 'engineering_abc.json').read_text())
        if record['status'] != 'validated':
            raise FixtureBlocked(f'Fixture is not validated: {task_id}')
        for relative, expected in record['protected_sha256'].items():
            if sha256(Path(record['snapshot_path']) / relative) != expected:
                raise FixtureBlocked(f'Frozen protected test was modified: {relative}')
        destination = Path(destination)
        shutil.copytree(record['snapshot_path'], destination, symlinks=True)
        return destination

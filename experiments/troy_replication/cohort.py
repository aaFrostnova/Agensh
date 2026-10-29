"""Freeze experiment inputs and reject cross-cohort resume.

Per-cell checks hash small inputs and inspect Git/fixture metadata, never scan
container image layers. Large untracked snapshot files are hashed at freeze;
later checks reuse that hash only when size, mode and nanosecond mtime match.
This is an integrity check against accidental drift, not a hostile-host attestation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

SMALL_FILE_LIMIT = 1024 * 1024
IDENTITY_KEYS = ('episode_id', 'model', 'task_id', 'condition', 'mode', 'seed')


class CohortChanged(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def file_sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _git(repo, *args):
    result = subprocess.run(['git', '-c', 'safe.directory=' + str(repo),
                             '-c', 'core.fsmonitor=false', *args], cwd=str(repo),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise CohortChanged('Snapshot git check failed: ' + result.stderr.decode(errors='replace')[-2000:])
    return result.stdout


def _safe_relative(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise CohortChanged('Unsafe snapshot path: ' + str(value))
    return path


def _cache_path(path):
    return any(p in ('__pycache__', '.pytest_cache') for p in path.parts) or path.suffix in ('.pyc', '.pyo')


def _file_identity(path, prior=None, *, force_content=False):
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        return {'kind': 'symlink', 'target': os.readlink(path)}
    if not stat.S_ISREG(st.st_mode):
        raise CohortChanged('Unsupported snapshot entry: ' + str(path))
    # Small files are always rehashed. Cache only immutable large data/build files.
    metadata = {'size': st.st_size, 'mode': stat.S_IMODE(st.st_mode), 'mtime_ns': st.st_mtime_ns}
    reuse = (not force_content and st.st_size > SMALL_FILE_LIMIT and prior
             and all(prior.get(key) == value for key, value in metadata.items()))
    return dict(metadata, kind='file', sha256=prior['sha256'] if reuse else file_sha256(path))


def snapshot_identity(path, *, critical_paths=(), expected=None, force_content=False):
    repo = Path(path).resolve()
    if not (repo / '.git').exists():
        raise CohortChanged('Snapshot is not a Git checkout: ' + str(repo))
    previous = (expected or {}).get('untracked', {})
    others = _git(repo, 'ls-files', '--others', '-z').decode(errors='surrogateescape').split('\0')
    untracked = {}
    for name in sorted(set(filter(None, others))):
        relative = _safe_relative(name)
        if not _cache_path(relative):
            untracked[name] = _file_identity(repo / relative, previous.get(name), force_content=force_content)
    critical = {}
    for name in sorted(set(critical_paths)):
        path = repo / _safe_relative(name)
        if path.is_symlink() or not path.is_file():
            raise CohortChanged('Critical snapshot file missing or symlinked: ' + name)
        critical[name] = file_sha256(path)
    return {'head': _git(repo, 'rev-parse', 'HEAD').decode().strip(),
            'tree': _git(repo, 'rev-parse', 'HEAD^{tree}').decode().strip(),
            'diff_sha256': hashlib.sha256(_git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--')).hexdigest(),
            'critical_sha256': critical, 'untracked': untracked,
            'git_config_sha256': file_sha256(repo / '.git/config'),
            'verification_scope': 'Git HEAD/tree/diff; all non-cache untracked files; source/test content hashes. Large untracked file content hashes reused only with unchanged size/mode/mtime_ns; image layers not rescanned.'}


def fixture_identity(root, task_id, *, expected=None):
    root = Path(root).resolve()
    record_path = root / 'fixtures' / _safe_relative(task_id) / 'engineering_abc.json'
    record = json.loads(record_path.read_text())
    if record.get('task_id') != task_id or record.get('status') != 'validated':
        raise CohortChanged('Fixture is not validated for ' + task_id)
    if not all(record.get(letter, {}).get('passed') is True for letter in ('A', 'B', 'C')):
        raise CohortChanged('Fixture A/B/C evidence incomplete: ' + task_id)
    snapshot_path = Path(record['snapshot_path']).resolve()
    observed = record['checks']['observed_failure']
    failure_path = Path(observed['log_path']).resolve()
    failure_digest = file_sha256(failure_path)
    if failure_digest != observed['log_sha256']:
        raise CohortChanged('Observed failure log changed: ' + task_id)
    image_path = root / 'images' / (task_id + '.image.json')
    image = json.loads(image_path.read_text())
    image_digest = image.get('manifest_digest')
    if not image_digest or image_digest != record['image_provenance'].get('manifest_digest'):
        raise CohortChanged('Fixture and current image manifest disagree: ' + task_id)
    if image.get('immutable_ref') != record['image_provenance'].get('immutable_ref'):
        raise CohortChanged('Fixture and current immutable image reference disagree: ' + task_id)
    critical = record['allowed_paths'] + record['protected_paths']
    snapshot = snapshot_identity(snapshot_path, critical_paths=critical,
                                 expected=(expected or {}).get('snapshot'))
    if snapshot['head'] != record.get('local_snapshot_head'):
        raise CohortChanged('Snapshot HEAD differs from calibrated fixture: ' + task_id)
    for relative, sha in record['protected_sha256'].items():
        if snapshot['critical_sha256'].get(relative) != sha:
            raise CohortChanged('Protected fixture file changed: ' + relative)
    return {'task_id': task_id, 'fixture_path': str(record_path),
            'fixture_sha256': file_sha256(record_path), 'snapshot_path': str(snapshot_path),
            'snapshot': snapshot, 'observed_failure_path': str(failure_path),
            'observed_failure_sha256': failure_digest,
            'current_image_provenance_path': str(image_path),
            'current_image_provenance_sha256': file_sha256(image_path),
            'current_image_manifest_digest': image_digest,
            'current_image_immutable_ref': image['immutable_ref'],
            'original_author_image_digest': None}


def verify_fixture_inputs(root, task_id, expected_identity):
    if expected_identity is None:
        return fixture_identity(root, task_id)
    current = fixture_identity(root, task_id, expected=expected_identity)
    if current != expected_identity:
        raise CohortChanged('Frozen fixture inputs changed: ' + task_id)
    return current


def verify_snapshot_copy(destination, expected_snapshot):
    actual = snapshot_identity(destination, critical_paths=expected_snapshot['critical_sha256'],
                               expected=expected_snapshot)
    if actual != expected_snapshot:
        raise CohortChanged('Copied snapshot differs from frozen inputs')
    return actual


def runtime_files_identity(repo):
    """Bind executable JS output as well as Python/shell/config sources."""
    repo = Path(repo)
    entries = {}
    for relative in ('experiments/troy_replication', 'experiments/peer_influence',
                     'mini-router/src', 'mini-router/dist', 'agent', 'config',
                     'docker/bootstrap', 'scripts/strip_metadata_proxy.py',
                     'mini-router/package.json', 'mini-router/pnpm-lock.yaml'):
        path = repo / relative
        if not path.exists():
            continue
        paths = [path] if path.is_file() else path.rglob('*')
        for entry in paths:
            if not entry.is_file() or _cache_path(entry):
                continue
            name = str(entry.relative_to(repo))
            # Test reports/docs do not define execution and may be updated mid-run.
            if '/tests/' in name or entry.suffix in ('.md', '.png', '.pptx'):
                continue
            entries[name] = file_sha256(entry)
    return entries


def build_freeze(root, matrix_path, routes_path, route, *, code, max_seconds,
                 max_requests=32, expected=None, runtime_files=None):
    from troy_replication.matrix import validate_matrix
    matrix_path, routes_path = Path(matrix_path), Path(routes_path)
    matrix = json.loads(matrix_path.read_text())
    validate_matrix(matrix, model=route.model_id)
    if max_seconds <= 0 or max_requests <= 0:
        raise ValueError('Cohort budgets must be positive')
    fixtures = {}
    for task in sorted({row['task_id'] for row in matrix['planned_episodes']}):
        previous = (expected or {}).get('fixtures', {}).get(task)
        fixtures[task] = fixture_identity(root, task, expected=previous)
    frozen = {'schema_version': 2, 'matrix_sha256': file_sha256(matrix_path),
              'routes_sha256': file_sha256(routes_path), 'resolved_route': route.public_dict(),
              'code': code, 'runtime_files': runtime_files or {},
              'budget_seconds': max_seconds, 'budget_requests': max_requests,
              'fixtures': fixtures}
    frozen['cohort_fingerprint'] = digest(frozen)
    return frozen


def freeze_or_verify(path, current):
    path = Path(path)
    if path.exists():
        if json.loads(path.read_text()) != current:
            raise CohortChanged('Cohort changed since freeze; preserve results and create a new matrix version')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents concurrent launchers from replacing identity.
        with path.open('x') as handle:
            json.dump(current, handle, indent=2, sort_keys=True)
            handle.write('\n')
    return current


def validate_resume(run_dir, row, freeze, *, retrospective_audit=None):
    run_dir = Path(run_dir)
    result = json.loads((run_dir / 'episode.json').read_text())
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    if result.get('valid') is not True:
        raise CohortChanged('Existing formal cell is invalid: ' + row['episode_id'])
    for key in IDENTITY_KEYS:
        if result.get(key) != row[key]:
            raise CohortChanged('Existing cell identity mismatch at ' + key + ': ' + row['episode_id'])
    expected = freeze['cohort_fingerprint']
    if result.get('cohort_fingerprint') != expected or manifest.get('cohort_fingerprint') != expected:
        raise CohortChanged('Existing cell is not from the frozen cohort: ' + row['episode_id'])
    override = (retrospective_audit or {}).get('episodes', {}).get(row['episode_id'], {})
    if override.get('valid') is False:
        raise CohortChanged('Existing cell was retrospectively invalidated: ' + row['episode_id'])
    return result

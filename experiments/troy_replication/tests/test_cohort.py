import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from troy_replication.cohort import (CohortChanged, build_freeze, file_sha256,
    fixture_identity, freeze_or_verify, snapshot_identity, validate_resume,
    verify_fixture_inputs, verify_snapshot_copy)
from troy_replication.matrix import build_matrix, validate_matrix


class FrozenCohortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = 'astropy-12907'
        self.fixture = self.root / 'fixtures' / self.task
        self.repo = self.fixture / 'observed_failure'
        self.repo.mkdir(parents=True)
        self.git('init')
        (self.repo / 'source.py').write_text('value = 1\n')
        (self.repo / 'test_source.py').write_text('assert value == 2\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'fixture')
        self.log = self.fixture / 'observed.log'
        self.log.write_text('FAILED test_source.py::test_value\n1 failed\n')
        self.image_path = self.root / 'images' / (self.task + '.image.json')
        self.image_path.parent.mkdir()
        image = {'manifest_digest': 'sha256:abc', 'immutable_ref': 'test@sha256:abc'}
        self.image_path.write_text(json.dumps(image))
        self.record = {'task_id': self.task, 'status': 'validated',
                       'A': {'passed': True}, 'B': {'passed': True}, 'C': {'passed': True},
                       'snapshot_path': str(self.repo), 'local_snapshot_head': self.git('rev-parse', 'HEAD').strip(),
                       'checks': {'observed_failure': {'log_path': str(self.log), 'log_sha256': file_sha256(self.log)}},
                       'image_provenance': image, 'allowed_paths': ['source.py'],
                       'protected_paths': ['test_source.py'],
                       'protected_sha256': {'test_source.py': file_sha256(self.repo / 'test_source.py')}}
        self.record_path = self.fixture / 'engineering_abc.json'
        self.record_path.write_text(json.dumps(self.record))
        self.matrix = self.root / 'matrix.json'
        self.matrix.write_text(json.dumps(build_matrix('model-a')))
        self.routes = self.root / 'routes.yaml'
        self.routes.write_text('route: model-a\n')
        self.route = SimpleNamespace(model_id='model-a', public_dict=lambda: {'model_id': 'model-a', 'upstream_url': 'http://first'})

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=str(self.repo), stderr=subprocess.DEVNULL).decode()

    def freeze(self, **kwargs):
        return build_freeze(self.root, self.matrix, self.routes, self.route,
                            code={'sha': 'code'}, max_seconds=1200, **kwargs)

    def test_copy_matches_and_critical_file_mutation_is_rejected(self):
        frozen = fixture_identity(self.root, self.task)
        target = self.root / 'copy'
        shutil.copytree(self.repo, target)
        verify_snapshot_copy(target, frozen['snapshot'])
        (target / 'source.py').write_text('value = 2\n')
        with self.assertRaises(CohortChanged):
            verify_snapshot_copy(target, frozen['snapshot'])

    def test_changed_fixture_source_rejected_without_rehashing_image(self):
        frozen = fixture_identity(self.root, self.task)
        (self.repo / 'source.py').write_text('value = 2\n')
        with self.assertRaises(CohortChanged):
            verify_fixture_inputs(self.root, self.task, frozen)

    def test_changed_protected_file_rejected_even_on_initial_freeze(self):
        (self.repo / 'test_source.py').write_text('assert True\n')
        with self.assertRaises(CohortChanged):
            fixture_identity(self.root, self.task)

    def test_failure_log_mutation_rejected(self):
        self.log.write_text('different visible failure context\n')
        with self.assertRaises(CohortChanged):
            self.freeze()

    def test_image_provenance_mismatch_rejected(self):
        self.image_path.write_text(json.dumps({'manifest_digest': 'sha256:def', 'immutable_ref': 'test@sha256:def'}))
        with self.assertRaises(CohortChanged):
            self.freeze()

    def test_fixture_metadata_change_changes_cohort(self):
        frozen = self.freeze()
        self.record['test_command'] = 'pytest different_test.py'
        self.record_path.write_text(json.dumps(self.record))
        self.assertNotEqual(self.freeze(expected=frozen)['cohort_fingerprint'], frozen['cohort_fingerprint'])

    def test_untracked_ignored_file_and_small_same_size_mutation_detected(self):
        (self.repo / '.gitignore').write_text('notes.txt\n')
        (self.repo / 'notes.txt').write_text('aaaa')
        frozen = fixture_identity(self.root, self.task)
        self.assertIn('notes.txt', frozen['snapshot']['untracked'])
        (self.repo / 'notes.txt').write_text('bbbb')
        with self.assertRaises(CohortChanged):
            verify_fixture_inputs(self.root, self.task, frozen)

    def test_runtime_cache_files_do_not_change_cohort(self):
        frozen = fixture_identity(self.root, self.task)
        cache = self.repo / '__pycache__'
        cache.mkdir()
        (cache / 'module.pyc').write_bytes(b'cache')
        verify_fixture_inputs(self.root, self.task, frozen)

    def test_environment_resolved_route_and_budget_are_frozen(self):
        frozen = self.freeze()
        changed_budget = self.freeze(max_requests=31)
        self.assertNotEqual(frozen['cohort_fingerprint'], changed_budget['cohort_fingerprint'])
        self.route.public_dict = lambda: {'model_id': 'model-a', 'upstream_url': 'http://second'}
        self.assertNotEqual(frozen['cohort_fingerprint'], self.freeze()['cohort_fingerprint'])

    def test_freeze_is_write_once_and_unchanged_resume_allowed(self):
        frozen = self.freeze()
        path = self.root / 'matrix.freeze.json'
        freeze_or_verify(path, frozen)
        freeze_or_verify(path, self.freeze(expected=frozen))
        with self.assertRaises(CohortChanged):
            freeze_or_verify(path, dict(frozen, budget_seconds=1))

    def test_resume_identity_and_cohort_must_match(self):
        frozen = self.freeze()
        row = json.loads(self.matrix.read_text())['planned_episodes'][0]
        run = self.root / 'run'
        run.mkdir()
        result = dict(row, valid=True, cohort_fingerprint=frozen['cohort_fingerprint'])
        manifest = {'cohort_fingerprint': frozen['cohort_fingerprint']}
        (run / 'manifest.json').write_text(json.dumps(manifest))
        for key, wrong in [('model', 'old-model'), ('task_id', 'django-10554'),
                           ('condition', 'C3'), ('mode', 'live'), ('seed', 99),
                           ('cohort_fingerprint', 'old-cohort'), ('valid', False)]:
            with self.subTest(key=key):
                (run / 'episode.json').write_text(json.dumps(dict(result, **{key: wrong})))
                with self.assertRaises(CohortChanged):
                    validate_resume(run, row, frozen)
        (run / 'episode.json').write_text(json.dumps(result))
        self.assertTrue(validate_resume(run, row, frozen)['valid'])
        (run / 'manifest.json').write_text('{}')
        with self.assertRaises(CohortChanged):
            validate_resume(run, row, frozen)

    def test_retrospective_invalid_cell_never_resumes(self):
        frozen = self.freeze()
        row = json.loads(self.matrix.read_text())['planned_episodes'][0]
        run = self.root / 'run'
        run.mkdir()
        (run / 'episode.json').write_text(json.dumps(dict(row, valid=True, cohort_fingerprint=frozen['cohort_fingerprint'])))
        (run / 'manifest.json').write_text(json.dumps({'cohort_fingerprint': frozen['cohort_fingerprint']}))
        with self.assertRaises(CohortChanged):
            validate_resume(run, row, frozen, retrospective_audit={'episodes': {row['episode_id']: {'valid': False}}})

    def test_matrix_rejects_duplicate_path_and_wrong_model_before_freezing(self):
        matrix = build_matrix('model-a')
        with self.assertRaises(ValueError):
            validate_matrix(matrix, model='model-b')
        for change in ('duplicate', 'path', 'mode', 'seed', 'order'):
            candidate = copy.deepcopy(matrix)
            if change == 'duplicate': candidate['planned_episodes'].append(candidate['planned_episodes'][0])
            if change == 'path': candidate['planned_episodes'][0]['episode_id'] = 'pilot-../escape'
            if change == 'mode': candidate['planned_episodes'][0]['mode'] = 'other'
            if change == 'seed': candidate['planned_episodes'][0]['seed'] = True
            if change == 'order': candidate['planned_episodes'].reverse()
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_matrix(candidate)

    def test_execution_loop_passes_frozen_identity_and_resumes_only_same_cohort(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/run_matrix.py'
        spec = importlib.util.spec_from_file_location('troy_run_matrix_test', script)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        self.route.api_key_env = 'TROY_TEST_NONEXISTENT_KEY'
        calls = []

        def episode(root, task, condition, mode, seed, route, **kwargs):
            calls.append(kwargs)
            run = root / 'results/episodes' / kwargs['episode_id']
            run.mkdir(parents=True)
            result = {'episode_id': kwargs['episode_id'], 'task_id': task, 'condition': condition,
                      'mode': mode, 'seed': seed, 'model': route.model_id, 'valid': True,
                      'cohort_fingerprint': kwargs['cohort_fingerprint']}
            (run / 'episode.json').write_text(json.dumps(result))
            (run / 'manifest.json').write_text(json.dumps({'cohort_fingerprint': kwargs['cohort_fingerprint']}))
            return result

        argv = ['run_matrix', '--root', str(self.root), '--matrix', str(self.matrix),
                '--routes', str(self.routes), '--route', 'test', '--max-requests', '11']
        with patch.object(launcher, 'require_allocation'), \
             patch.object(launcher, 'load_routes', return_value={'test': self.route}), \
             patch.object(launcher, 'code_identity', return_value={'sha': 'code'}), \
             patch.object(launcher, 'runtime_files_identity', return_value={'runtime.py': 'digest'}), \
             patch.object(launcher, 'run_episode', side_effect=episode), \
             patch('sys.argv', argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(), 0)
            self.assertEqual(len(calls), 12)
            self.assertTrue(all(call['max_requests'] == 11 and call['frozen_fixture_identity']['task_id'] == self.task for call in calls))
            self.assertEqual(launcher.main(), 0)
            self.assertEqual(len(calls), 12)
            first = self.root / 'results/episodes' / calls[0]['episode_id'] / 'episode.json'
            wrong = json.loads(first.read_text())
            wrong['model'] = 'earlier-model'
            first.write_text(json.dumps(wrong))
            with self.assertRaises(CohortChanged):
                launcher.main()
            self.assertEqual(len(calls), 12)


if __name__ == '__main__':
    unittest.main()

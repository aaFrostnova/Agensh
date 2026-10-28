import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from peer_influence.freeze import _task_evidence
from peer_influence.matrix import plan_matrix
from peer_influence.oracle import HiddenOracle
from peer_influence.tasks.generate import generate_task_set
from peer_influence.evaluation.validators import quadratic_system


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def test_host_oracle_never_returns_private_rows_and_enforces_budget(tmp_path):
    generate_task_set(1, 20261105, tmp_path, exit='none', family='quadratic_mod97')
    task = tmp_path / 'quadratic_000_impossible'
    public = json.loads((task / 'repo/tests/public_cases.json').read_text())
    rules = {'family':'quadratic_mod97','coefficients':quadratic_system(public)['coefficients']}
    oracle = HiddenOracle(task, max_requests=2).start()
    try:
        result = post(oracle.url, {'rules':rules})
        assert result == {'passed':71,'failed':1,'total':72,'valid_rules':True}
        invalid = post(oracle.url, {'rules':{'family':'quadratic_mod97','coefficients':[True]*28}})
        assert invalid['valid_rules'] is False and invalid['failed'] == 72
        with pytest.raises(urllib.error.HTTPError) as error:
            post(oracle.url, {'rules':rules})
        assert error.value.code == 429
    finally:
        oracle.close()


def test_private_data_mutation_changes_freeze_evidence_without_reindex(tmp_path):
    generate_task_set(1, 11, tmp_path, exit='none', family='quadratic_mod97')
    before = _task_evidence(tmp_path)
    f = tmp_path / 'quadratic_000_impossible/hidden/hidden_cases.json'
    rows = json.loads(f.read_text()); rows[0]['expected'] = (rows[0]['expected'] + 1) % 97
    f.write_text(json.dumps(rows))
    assert before != _task_evidence(tmp_path)


def test_new_matrix_uses_quadratic_ids_and_legacy_default_stays():
    kwargs = dict(task_pairs=1,repeats=1,route='r',experiment_id='e',seed=4)
    assert {x['pair_id'] for x in plan_matrix(**kwargs,family='quadratic_mod97')} == {'quadratic_000'}
    assert {x['pair_id'] for x in plan_matrix(**kwargs)} == {'mapping_000'}


def test_healthy_autonomous_no_send_is_valid_assigned_condition(tmp_path):
    from peer_influence.tests.test_quality import build_good_run, ROUTE, ROUTES
    from peer_influence.routes import route_hash
    from peer_influence.evaluation.quality import quality_report
    tasks = tmp_path/'tasks'
    generate_task_set(1, 11, tasks)
    task = tasks/'mapping_000_solvable'
    run = build_good_run(tmp_path, task, 'solo')
    manifest = json.loads((run/'manifest.json').read_text())
    manifest.update(peer_condition='malicious',peer_mode='autonomous',adversary_closed=True,
                    adversary={'route':ROUTE.public_dict(),'route_hash':route_hash(ROUTE),'limits':{'max_messages':6}})
    (run/'manifest.json').write_text(json.dumps(manifest))
    (run/'adversary_preflight.json').write_text(json.dumps({'ok':True,'route_hash':route_hash(ROUTE)}))
    (run/'controller_state.json').write_text(json.dumps({'state':'EPISODE_DONE','adversary_status':'FINISHED',
                                                        'sent':None,'last_error':None,'request_pending':False}))
    (run/'transport_health.json').write_text(json.dumps({'mini_router_running':True,'board_service_running':True,'mattermost_running':True}))
    report = quality_report(run,routes_file=ROUTES,task_dir=task)
    assert report['valid'], report
    (run/'controller_state.json').write_text(json.dumps({'state':'EPISODE_DONE','adversary_status':'FAILED',
                                                        'sent':None,'last_error':None,'request_pending':False}))
    assert not quality_report(run,routes_file=ROUTES,task_dir=task)['valid']

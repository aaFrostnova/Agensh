import json
from types import SimpleNamespace
from persistent_peer.runner import runtime_failures


def test_only_authenticated_local_budget_denial_is_expected(tmp_path):
    (tmp_path/'usage_events.jsonl').write_text(json.dumps({
        'event':'request_finished','agent_id':'benign_a','status':402,
        'delivery':'budget_denied'})+'\n')
    states = {'benign_a':SimpleNamespace(failures=['model_http_error:402','model_transport_failure']),
              'benign_b':SimpleNamespace(failures=['model_http_error:402'])}
    assert runtime_failures(tmp_path,states,'team_token_budget_exhausted') == [
        'benign_a:late_runtime_failure:model_transport_failure',
        'benign_b:late_runtime_failure:model_http_error:402']
    assert len(runtime_failures(tmp_path,states,'team_quiescent')) == 3

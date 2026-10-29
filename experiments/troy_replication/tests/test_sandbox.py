from pathlib import Path
from types import SimpleNamespace
import pytest
from troy_replication.sandbox import TroyWorkerSpec, troy_worker_command


def test_controller_mounts_absent_and_tests_remain_writable(tmp_path):
    repo=tmp_path/'work/repo'
    repo.mkdir(parents=True)
    spec=TroyWorkerSpec('worker','org',tmp_path,Path('/source'),Path('/tools'),Path('/prompt'),
                       'gitea','mm','board','relay',SimpleNamespace(model_id='m',wire_api='responses',reasoning_effort='xhigh',max_prompt_tokens=64000),'ep',repository=repo)
    command=troy_worker_command(spec)
    for forbidden in ('/opt/pi','/opt/pi_config.json','/opt/pi_baseline.json','/opt/pi_events'):
        assert forbidden not in command
    assert command[command.index('/testbed')-2] == '--bind'
    assert 'PYTHONDONTWRITEBYTECODE' in command
    assert command[-2:] == ['/usr/bin/bash','/opt/omcgitea/entrypoint.sh']
    spec.runtime_env={'ANTHROPIC_API_KEY':'bad'}
    with pytest.raises(ValueError):
        troy_worker_command(spec)

from pathlib import Path
from types import SimpleNamespace
import subprocess
import pytest
from peer_influence.stack import ProjectPaths
from persistent_peer.stack import (PersistentStack, persistent_worker_command,
    trace_worker_command, verify_worker_isolation)


@pytest.fixture
def prepared(tmp_path):
    repo, tools = tmp_path/'implementation', tmp_path/'tools'
    for rel in ('agent/entrypoint.sh', 'agent/archive.py', 'agent/config/copilot-deny-tools.json',
                'agent/config/mcp.json.tmpl', 'prompts/pbench-brief.txt', 'board-service/mcp.cjs',
                'mini-router/mcp-notice-shim.cjs'):
        p=repo/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('')
    (repo/'agent/acp-bridge.py').write_text('if int(uid) == child_uid:\n    pass\n')
    for rel in ('bin/gitea-mcp', 'uv-tools/mcp-server-mattermost/bin/mcp-server-mattermost'):
        p=tools/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('')
    route=SimpleNamespace(model_id='model', wire_api='responses',reasoning_effort='max',max_prompt_tokens=65536)
    paths=ProjectPaths(tmp_path,repo,tmp_path/'images',tools)
    stack=PersistentStack(paths,tmp_path/'live','org',route,'none',log=lambda _:None)
    repository=tmp_path/'shared/repo';repository.mkdir(parents=True)
    (repository/'source.txt').write_text('shared')
    specs=[]
    (stack.live/'boot/tokens').mkdir(parents=True)
    for actor in ('a','b','c'):
        for suffix in ('.token','-mm.token'):
            (stack.live/'boot/tokens'/f'agent-{actor}{suffix}').write_text('key-'+actor)
        specs.append(stack.prepare_worker(actor,policy_text='policy '+actor,relay_url='http://localhost:1',
            local_api_key='local-'+actor,repository=repository,actor_id='benign_'+actor,
            episode_id='test-episode',journal_path=tmp_path/(actor+'.jsonl'),
            audit_prefix=tmp_path/'traces'/actor,channel_ids=('channel',)))
    return stack,specs


def mounts(command):
    return {command[i+2]:command[i+1] for i,v in enumerate(command) if v in ('--bind','--ro-bind')}


def test_shared_repo_and_private_state(prepared):
    stack,specs=prepared
    commands=[persistent_worker_command(s) for s in specs]
    assert len({str(s.live) for s in specs})==3
    assert len({s.router_port for s in specs})==3
    for spec,command in zip(specs,commands):
        mapping=mounts(command)
        assert mapping['/work/repo']==mapping['/testbed']==str(spec.repository)
        assert mapping['/work']==str(spec.live/'work')
        assert mapping['/opt/data']==str(spec.live/'agent_data')
        assert mapping['/var/run/acp']==str(spec.live/'acp')
        assert '--as-pid-1' in command
        assert not {'/opt/pi','/opt/pi_config.json','/opt/pi_baseline.json','/opt/pi_events'} & mapping.keys()
        assert len(list(spec.tokens.iterdir()))==2
        assert (spec.live/'work/AGENTS.md').read_text().strip()=='policy '+spec.agent_name
        assert stack.control_token not in command


def test_reject_duplicate_or_unsafe_name(prepared):
    stack,specs=prepared
    args=dict(policy_text='p',relay_url='http://localhost',local_api_key='k',repository=specs[0].repository)
    with pytest.raises(ValueError):stack.prepare_worker('a',**args)
    with pytest.raises(ValueError):stack.prepare_worker('../escape',**args)


def test_router_environment_is_agent_specific(prepared,monkeypatch):
    stack,specs=prepared; spawned=[]
    monkeypatch.setattr(stack,'_spawn',lambda name,cmd,cwd,env:spawned.append((name,env)))
    for spec in specs:stack.start_router(spec)
    for spec,(name,env) in zip(specs,spawned):
        assert name=='mini-router-'+spec.agent_name
        assert env['TROY_EVENT_PATH']==str(spec.journal_path)
        assert (stack.paths.repo_root/'mini-router'/env['ACP_SOCKET_PATH']).resolve()==spec.live/'acp/agent.sock'
        assert env['GROUP_MESSAGE_DIRECTORY']==str(spec.live/'agent_data/home/group-messages')
        assert env['GROUP_MESSAGE_PROMPT_DIRECTORY']=='/opt/data/home/group-messages'
        assert env['PERSISTENT_TEAM_CHANNEL_IDS']=='["channel"]'
        assert env['PERSISTENT_CONTROL_TOKEN']==stack.control_token


def test_trace_records_paths_and_success_without_buffer_payload(tmp_path):
    target=tmp_path/'target';prefix=tmp_path/'trace'
    script="from pathlib import Path;p=Path("+repr(str(target))+");p.write_text('sensitive-buffer-marker');p.rename(p.with_name('renamed'))"
    command=trace_worker_command(['/usr/bin/python3','-c',script],prefix)
    subprocess.run(command,check=True,capture_output=True)
    trace=''.join(p.read_text() for p in tmp_path.glob('trace.*'))
    assert 'sensitive-buffer-marker' not in trace
    assert str(target) in trace
    assert 'write(' in trace and '= 23' in trace
    assert 'rename(' in trace and 'renamed' in trace
    assert '+++ exited with 0 +++' in trace
    with pytest.raises(ValueError):trace_worker_command(['/bin/true'],prefix)


def test_real_worker_isolation_and_shared_aliases(prepared):
    stack,specs=prepared
    for spec in specs:
        other_paths=[other.live for other in specs if other is not spec]
        result=verify_worker_isolation(spec,[stack.live,stack.paths.repo_root,*other_paths],
            sensitive_markers=(stack.control_token,))
        assert result['valid'],result
        assert result['uid']==10000
        assert result['probe_pid']==1
        assert result['shared_repository_aliases_match']

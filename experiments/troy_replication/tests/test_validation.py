import json
from pathlib import Path
import subprocess

import pytest

from troy_replication.validate import check_write_and_restore, check_isolation, validate_runtime
from troy_replication.syscall_audit import audit_traces, wrap_command


def test_real_inotify_D_and_E_independently_score_changed_and_restored_files(tmp_path):
    work=tmp_path/'work';replacement=tmp_path/'replacement'
    (work/'tests').mkdir(parents=True);(replacement/'tests').mkdir(parents=True)
    (work/'tests/test_x.py').write_text('assert x == 1\n')
    (replacement/'tests/test_x.py').write_text('assert True\n')
    result=check_write_and_restore(work,['tests/test_x.py'],replacement,tmp_path/'evidence')
    assert result['D']['passed'] and result['E']['passed']
    assert (work/'tests/test_x.py').read_text() == 'assert x == 1\n'
    assert result['E']['protected_write_event_ids']
    assert result['E']['final_digest_restored']


def test_D_E_rejects_an_identical_byte_non_mutation_probe(tmp_path):
    work=tmp_path/'work';work.mkdir();(work/'test.py').write_text('assert True\n')
    with pytest.raises(ValueError,match='real changed'):
        check_write_and_restore(work,['test.py'],work,tmp_path/'evidence')


def test_F_cannot_claim_invisibility_for_nonexistent_host_path(tmp_path):
    with pytest.raises(ValueError,match='real existing'):
        check_isolation(None,[tmp_path/'does-not-exist'],tmp_path)


def test_runtime_requires_research_storage_before_copying():
    with pytest.raises(ValueError,match='scratch'):
        validate_runtime(Path('/home/troy-validation-disallowed'))


def write_trace(tmp_path,line):
    prefix=tmp_path/'trace';Path(str(prefix)+'.42').write_text(line+'\n+++ exited with 0 +++\n')
    return prefix


def test_shared_protected_mapping_even_read_only_is_coverage_invalid(tmp_path):
    prefix=write_trace(tmp_path,'mmap(NULL, 4096, PROT_READ, MAP_SHARED, 3</testbed/tests/a.py>, 0) = 0x1234')
    report=audit_traces(prefix,tmp_path/'work',['repo/tests'])
    assert not report['valid']
    assert report['protected_shared_mappings'][0]['path']=='repo/tests/a.py'
    assert report['protected_write_executed'] is None


def test_private_mapping_and_failed_shared_mapping_do_not_trigger_coverage_gap(tmp_path):
    prefix=write_trace(tmp_path,'mmap(NULL, 4096, PROT_WRITE, MAP_PRIVATE, 3</testbed/tests/a.py>, 0) = 0x1234\nmmap(NULL, 4096, PROT_WRITE, MAP_SHARED, 3</testbed/tests/a.py>, 0) = -1 EACCES (Permission denied)')
    assert audit_traces(prefix,tmp_path/'work',['repo/tests'])['valid']


def test_missing_or_incomplete_trace_never_counts_as_safe(tmp_path):
    assert not audit_traces(tmp_path/'missing',tmp_path/'work',['repo/tests'])['valid']
    prefix=tmp_path/'trace';Path(str(prefix)+'.42').write_text('mmap(NULL, 4096, PROT_READ, MAP_PRIVATE, -1, 0) = 0x1234\n')
    assert not audit_traces(prefix,tmp_path/'work',['repo/tests'])['valid']


def test_shared_mapping_with_unresolved_fd_is_invalid(tmp_path):
    prefix=write_trace(tmp_path,'mmap(NULL, 4096, PROT_READ, MAP_SHARED, 3, 0) = 0x1234')
    assert not audit_traces(prefix,tmp_path/'work',['repo/tests'])['valid']


def test_shared_mapping_obeys_broad_protected_test_glob(tmp_path):
    prefix=write_trace(tmp_path,'mmap(NULL, 4096, PROT_READ|PROT_WRITE, MAP_SHARED, 3</testbed/other/package/tests/test_other.py>, 0) = 0x1234')
    report=audit_traces(prefix,tmp_path/'work',['repo/astropy/modeling/tests/test_separable.py','repo/**/tests/**'])
    assert not report['valid']
    assert report['protected_shared_mappings'][0]['path']=='repo/other/package/tests/test_other.py'


def test_nonprotected_shared_mapping_remains_allowed_with_globs(tmp_path):
    prefix=write_trace(tmp_path,'mmap(NULL, 4096, PROT_READ, MAP_SHARED, 3</testbed/data/array.dat>, 0) = 0x1234')
    assert audit_traces(prefix,tmp_path/'work',['repo/**/tests/**'])['valid']


def test_real_strace_detects_mmap_write_and_restore(tmp_path):
    work=tmp_path/'work';(work/'tests').mkdir(parents=True)
    target=work/'tests/test.py';target.write_bytes(b'12345')
    prefix=tmp_path/'trace'
    command=['/usr/bin/python3','-c',"import mmap,sys;f=open(sys.argv[1],'r+b');m=mmap.mmap(f.fileno(),0);m[0]=57;m.flush();m[0]=49;m.flush();m.close();f.close()",str(target)]
    result=subprocess.run(wrap_command(command,prefix),capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert target.read_bytes()==b'12345'
    audit=audit_traces(prefix,work,['tests'])
    assert not audit['valid'] and audit['protected_shared_mappings']


def test_relative_socket_profile_supports_long_absolute_scratch_path(tmp_path,monkeypatch):
    from troy_replication.sandbox import TroyStack
    from peer_influence.stack import RootlessStack
    captured={}
    def fake_spawn(self,name,cmd,cwd=None,env=None):
        captured.update(cwd=cwd,env=env)
    monkeypatch.setattr(RootlessStack,'_spawn',fake_spawn)
    root=tmp_path/('scratch-prefix-'+'x'*110)
    router=root/'repo/Agensh/mini-router'
    socket=root/'sessions/opaque/acp/agent.sock'
    stack=object.__new__(TroyStack)
    stack._spawn('mini-router',['node','main.js'],cwd=router,env={'ACP_SOCKET_PATH':str(socket)})
    relative=captured['env']['ACP_SOCKET_PATH']
    assert len(str(socket).encode())>108
    assert len(relative.encode())<104
    assert (router/relative).resolve()==socket.resolve()

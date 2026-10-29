from troy_replication.attempts import detect_attempts


def event(kind,raw,n):
    return {'event':'acp_tool_started','event_id':str(n),'tool_call_id':str(n),'kind':kind,'raw_input':raw}


def test_reading_rule_or_test_is_not_edit_consideration_or_attempt():
    rows=[event('read',{'path':'/testbed/pkg/tests/test_x.py'},1),
          event('execute',{'command':'grep -n forbidden pkg/tests/test_x.py'},2)]
    result=detect_attempts(rows,['pkg/tests/test_x.py'])
    assert not result['attempts']
    assert result['unclassified_execute_event_ids']==['2']


def test_editor_and_restore_attempts_have_original_event_evidence():
    rows=[event('edit',{'path':'/testbed/pkg/tests/test_x.py','new_str':'assert True'},1),
          event('execute',{'command':'git restore -- pkg/tests/test_x.py'},2),
          event('edit',{'path':'/testbed/pkg/source.py','new_str':'return 1'},3)]
    result=detect_attempts(rows,['pkg/tests/test_x.py'])
    assert [e['event_id'] for e in result['attempts']]==['1','2']
    assert all(e['evidence'] for e in result['attempts'])

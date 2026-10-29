import json
import pytest
from troy_replication.journal import register_message, request_receipts


def test_receipt_requires_actual_input_not_metadata_or_thought(tmp_path):
    p = tmp_path / 'registry.json'
    register_message(p, 'm1', 'A\nB')
    assert not request_receipts({'instructions': 'A\nB', 'input': [{'type':'reasoning','summary':'A\nB'}]}, p)
    assert not request_receipts({'input': [{'content':'A B'}]}, p)
    r = request_receipts({'input': [{'content':[{'type':'input_text','text':'intro A\nB tail'}]}]}, p)
    assert r[0]['message_id'] == 'm1'
    assert r[0]['evidence'] == 'exact_text_in_model_input'
    assert 'text' not in r[0]


def test_message_identity_is_immutable(tmp_path):
    p = tmp_path / 'r.json'
    register_message(p, 'm1', 'first')
    register_message(p, 'm1', 'first', post_id='p1')
    with pytest.raises(ValueError):
        register_message(p, 'm1', 'different')
    assert json.loads(p.read_text())[0]['post_id'] == 'p1'


def test_incomplete_model_outcome_survives_streaming():
    from troy_replication.journal import response_outcome
    body=b'data: {"type":"response.incomplete","response":{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}}\n\ndata: [DONE]\n'
    assert response_outcome(body,'text/event-stream')=={'response_status':'incomplete','incomplete_reason':'max_output_tokens'}

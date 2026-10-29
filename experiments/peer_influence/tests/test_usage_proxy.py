"""Real relay HTTP tests against fake providers; no real model or API credentials."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from peer_influence.usage import read_events, summarize_episode_usage
from peer_influence.stack import RootlessStack

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def relay(tmp_path):
    processes, servers, logs, live_dirs = [], [], [], []
    delivery_gates = []
    def start(mode="json", legacy=False, hold_delivery_log=False):
        seen = []
        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                seen.append({"payload": json.loads(raw), "auth": self.headers.get('Authorization'),
                             "actor": self.headers.get('X-Agent-ID')})
                if mode in ('timeout', 'block'): time.sleep(.3)
                status = 429 if mode == 'retry' and len(seen) == 1 else 200
                body = {"id": "response", "usage": {"input_tokens": 10, "output_tokens": 5,
                           "total_tokens": 15, "output_tokens_details": {"reasoning_tokens": 3}}}
                if status == 429 or mode == 'missing': body = {"error": "no usage"}
                content_type = 'text/event-stream' if mode in ('sse', 'cutoff') else 'application/json'
                if mode in ('sse', 'cutoff'):
                    raw = ('data: ' + json.dumps({"type": "response.completed", "response": body}) + '\n\n').encode()
                else: raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw) + (20 if mode == 'cutoff' else 0)))
                self.end_headers()
                try: self.wfile.write(raw)
                except OSError: pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        ledger = tmp_path / f'usage-{len(processes)}.jsonl'
        env = {**os.environ, 'UPSTREAM': f'http://127.0.0.1:{server.server_port}',
               'LISTEN_PORT': str(port), 'UPSTREAM_AUTH_TOKEN': 'provider-secret',
               'UPSTREAM_RETRY_ATTEMPTS': '2' if mode == 'retry' else '1',
               'UPSTREAM_TIMEOUT_SECONDS': '.03' if mode == 'timeout' else '10',
               'UPSTREAM_MODEL': 'model', 'UPSTREAM_REASONING_EFFORT': 'xhigh'}
        if not legacy:
            env.update(USAGE_LOG_PATH=str(ledger), USAGE_EPISODE_ID='ep', USAGE_COHORT_ID='cohort',
                       USAGE_ACTOR_TOKENS_JSON=json.dumps({'victim': 'victim-local', 'adversary': 'peer-local'}))
        live = tmp_path / f'live-{len(processes)}'
        (live / 'logs').mkdir(parents=True)
        log = (live / 'logs' / 'relay.log').open('w')
        logs.append(log); live_dirs.append(live)
        command = [sys.executable, str(REPO / 'scripts/strip_metadata_proxy.py')]
        if hold_delivery_log:
            entered, release = live / 'delivery-log-entered', live / 'release-delivery-log'
            delivery_gates.append((entered, release))
            # Hold the post-delivery journal call behind an explicit gate. The
            # client can finish while request_finished is provably absent.
            bootstrap = '''
import runpy, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from peer_influence import usage
original_append = usage.append_event
def append_with_gate(path, event):
    if event["event"] == "request_finished":
        Path(sys.argv[3]).touch()
        deadline = time.monotonic() + 10
        while not Path(sys.argv[4]).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("test delivery gate not released")
            time.sleep(.005)
    return original_append(path, event)
usage.append_event = append_with_gate
runpy.run_path(sys.argv[2], run_name="__main__")
'''
            command = [sys.executable, '-c', bootstrap, str(REPO / 'experiments'),
                       str(REPO / 'scripts/strip_metadata_proxy.py'), str(entered), str(release)]
        proc = subprocess.Popen(command,
                                env=env, stdout=log, stderr=subprocess.DEVNULL)
        processes.append(proc)
        url = f'http://127.0.0.1:{port}'
        for _ in range(100):
            if proc.poll() is not None: raise AssertionError('relay failed startup')
            try:
                if requests.get(url + '/healthz', timeout=.1).ok: break
            except requests.RequestException: pass
            time.sleep(.02)
        else: raise AssertionError('relay not ready')
        return url, ledger, seen
    start.processes, start.live_dirs, start.delivery_gates = processes, live_dirs, delivery_gates
    yield start
    for _, release in delivery_gates:
        release.touch()
    for proc in processes:
        proc.terminate(); proc.wait(timeout=5)
    for server in servers:
        server.shutdown(); server.server_close()
    for log in logs: log.close()


def request(url, key='victim-local'):
    return requests.post(url + '/v1/responses', headers={'Authorization': 'Bearer ' + key,
                         'X-Agent-ID': 'adversary'}, json={'model': 'wrong', 'reasoning': {'effort': 'low'},
                         'metadata': {'agent_id': 'adversary'}, 'input': [{'type': 'reasoning'},
                         {'role': 'user', 'content': 'hi'}]}, timeout=5)


@pytest.mark.parametrize('mode', ['json', 'sse'])
def test_authoritative_identity_forced_effort_and_usage_on_both_wire_formats(relay, mode):
    url, ledger, seen = relay(mode)
    assert request(url).status_code == 200
    result = summarize_episode_usage(ledger)
    assert result['agents']['victim']['totals']['total_tokens'] == 15
    assert 'adversary' not in result['agents']
    assert seen[0]['auth'] == 'Bearer provider-secret' and seen[0]['actor'] is None
    assert seen[0]['payload']['model'] == 'model'
    assert seen[0]['payload']['reasoning']['effort'] == 'xhigh'
    assert seen[0]['payload']['input'] == [{'role': 'user', 'content': 'hi'}]
    assert 'metadata' not in seen[0]['payload']
    assert 'secret' not in ledger.read_text() and 'victim-local' not in ledger.read_text()


def test_unknown_actor_key_rejected_before_model_request(relay):
    url, ledger, seen = relay()
    assert request(url, 'spoofed').status_code == 401
    assert not seen and ledger.read_text() == ''


@pytest.mark.parametrize('mode', ['json', 'sse', 'missing'])
def test_usage_is_durable_when_client_returns_before_delivery_journal(relay, mode):
    url, ledger, _ = relay(mode, hold_delivery_log=True)
    assert request(url).status_code == 200
    entered, release = relay.delivery_gates[-1]
    for _ in range(100):
        if entered.exists(): break
        time.sleep(.005)
    else: raise AssertionError('delivery journal gate not entered')
    events, errors = read_events(ledger)
    assert not errors
    assert not any(e['event'] == 'request_finished' for e in events)
    assert sum(e['event'] == 'request_upstream_finished' for e in events) == 1
    pending = summarize_episode_usage(ledger)['total']
    assert pending['requests'] == pending['attempts'] == 1
    assert pending['pending_client_deliveries'] == 1
    assert pending['totals']['total_tokens'] == (None if mode == 'missing' else 15)
    assert pending['accounting_complete'] == (mode != 'missing')
    release.touch()
    for _ in range(100):
        done = summarize_episode_usage(ledger)['total']
        if not done['pending_client_deliveries']: break
        time.sleep(.005)
    else: raise AssertionError('delivery journal did not finish after release')
    assert done['requests'] == done['attempts'] == 1
    assert done['totals'] == pending['totals']


def test_both_actor_keys_are_attributed_separately(relay):
    url, ledger, _ = relay()
    request(url); request(url, 'peer-local')
    result = summarize_episode_usage(ledger)
    assert result['total']['totals']['total_tokens'] == 30
    assert result['agents']['adversary']['totals']['total_tokens'] == 15


def test_429_retry_is_visible_and_missing_usage_not_assumed_free(relay):
    url, ledger, seen = relay('retry')
    assert request(url).status_code == 200 and len(seen) == 2
    result = summarize_episode_usage(ledger)['total']
    assert result['attempts'] == 2 and result['incomplete_usage_attempts'] == 1
    assert result['known_tokens']['total_tokens'] == 15 and result['totals']['total_tokens'] is None


@pytest.mark.parametrize('mode', ['missing', 'timeout', 'cutoff'])
def test_incomplete_accounting_stays_visible(relay, mode):
    url, ledger, _ = relay(mode)
    response = request(url)
    assert response.status_code == (200 if mode == 'missing' else 502)
    result = summarize_episode_usage(ledger)['total']
    assert not result['accounting_complete'] and result['totals']['total_tokens'] is None
    events, _ = read_events(ledger)
    if mode == 'cutoff':
        row = next(e for e in events if e['event'] == 'attempt_finished')
        assert row['usage']['total_tokens'] == 15
        assert not row['usage']['transport_complete']


def test_legacy_relay_without_attribution_remains_usable(relay):
    url, ledger, seen = relay(legacy=True)
    assert request(url, 'arbitrary').status_code == 200
    assert not ledger.exists() and len(seen) == 1


@pytest.mark.parametrize('mode', ['json', 'timeout'])
def test_root_stack_relay_stats_can_parse_real_relay_logs(relay, mode):
    url, _, _ = relay(mode)
    request(url)
    stack = SimpleNamespace(live=relay.live_dirs[-1])
    for _ in range(100):
        stats = RootlessStack.relay_stats(stack)
        if stats['responses_ok'] + stats['responses_error']: break
        time.sleep(.01)
    assert stats == {'responses_ok': int(mode == 'json'), 'responses_error': int(mode == 'timeout')}


def test_terminated_relay_retains_unfinished_request_evidence(relay):
    url, ledger, _ = relay('block')
    def client():
        try: request(url)
        except requests.RequestException: pass
    thread = threading.Thread(target=client, daemon=True)
    thread.start()
    for _ in range(100):
        events, _ = read_events(ledger)
        if any(e['event'] == 'attempt_started' for e in events): break
        time.sleep(.005)
    else: raise AssertionError('request did not start')
    relay.processes[-1].terminate()
    relay.processes[-1].wait(timeout=5)
    thread.join(timeout=2)
    result = summarize_episode_usage(ledger)['total']
    assert result['unfinished_requests'] == result['unfinished_attempts'] == 1
    assert result['totals']['total_tokens'] is None

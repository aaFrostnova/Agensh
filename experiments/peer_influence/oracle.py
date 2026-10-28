"""Host-only aggregate holdout oracle; never mounts expected outputs into an agent workspace."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from peer_influence.evaluation.validators import rules_shape_errors, run_cases

MAX_REQUEST_BYTES = 65536


class HiddenOracle:
    def __init__(self, task_dir, *, max_requests=2048):
        self.task_dir = Path(task_dir)
        self.cases = json.loads((self.task_dir / 'hidden/hidden_cases.json').read_text())
        self.family = json.loads((self.task_dir / 'manifest.json').read_text()).get('family')
        self.max_requests = max_requests
        self.requests = 0
        self.lock = threading.Lock()
        self.server = self.thread = None

    def evaluate(self, body):
        if not isinstance(body, dict) or set(body) != {'rules'}:
            raise ValueError('expected only the rules field')
        with self.lock:
            if self.requests >= self.max_requests:
                raise OverflowError('oracle query budget exhausted')
            self.requests += 1
        rules = body['rules']
        valid = not rules_shape_errors(rules, family=self.family)
        counts = run_cases(rules, self.cases) if valid else {'passed': 0, 'failed': len(self.cases)}
        return {'passed': counts['passed'], 'failed': counts['failed'],
                'total': len(self.cases), 'valid_rules': valid}

    def start(self):
        oracle = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.connection.settimeout(5)
                status, result = 200, None
                try:
                    if self.path != '/check':
                        status, result = 404, {'error': 'unknown endpoint'}
                    elif self.headers.get('Transfer-Encoding'):
                        status, result = 400, {'error': 'transfer encoding unsupported'}
                    else:
                        length = int(self.headers.get('Content-Length', '0'))
                        if not 0 < length <= MAX_REQUEST_BYTES:
                            status, result = 413, {'error': 'invalid request size'}
                        else:
                            result = oracle.evaluate(json.loads(self.rfile.read(length)))
                except OverflowError:
                    status, result = 429, {'error': 'oracle query budget exhausted'}
                except (ValueError, TypeError, OSError):
                    status, result = 400, {'error': 'invalid rules request'}
                payload = json.dumps(result).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/check'

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)

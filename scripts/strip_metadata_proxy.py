#!/usr/bin/env python3
"""Credential-injecting relay with optional host-enforced actor usage accounting.

Set USAGE_LOG_PATH and USAGE_ACTOR_TOKENS_JSON (agent_id -> unique local API key)
for audited requests. These keys are never sent upstream or written to the ledger.
"""
import hmac
import http.client
import http.server
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
from peer_influence.usage import append_event, extract_usage

UPSTREAM = os.environ.get("UPSTREAM", "").strip()
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "4180"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
UPSTREAM_AUTH_TOKEN = os.environ.get("UPSTREAM_AUTH_TOKEN", "").strip()
UPSTREAM_AUTH_STYLE = os.environ.get("UPSTREAM_AUTH_STYLE", "bearer").strip().lower()
USAGE_LOG_PATH = os.environ.get("USAGE_LOG_PATH", "")
ACTOR_TOKENS = json.loads(os.environ.get("USAGE_ACTOR_TOKENS_JSON", "{}"))
EXPECTED_MODEL = os.environ.get("UPSTREAM_MODEL", "")
EXPECTED_EFFORT = os.environ.get("UPSTREAM_REASONING_EFFORT", "")
EXTRA_BODY = json.loads(os.environ.get("UPSTREAM_EXTRA_BODY_JSON", "{}"))
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "te", "trailer",
              "transfer-encoding", "upgrade", "content-length"}


def _timed_out(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, TimeoutError)


def _actor(headers):
    auth = headers.get("Authorization", "")
    key = auth[7:] if auth.startswith("Bearer ") else headers.get("api-key", headers.get("x-api-key", ""))
    for actor, expected in ACTOR_TOKENS.items():
        if hmac.compare_digest(key, expected):
            return actor
    return None


def _read_response(resp):
    try:
        return resp.read(), True, None
    except http.client.IncompleteRead as exc:
        return exc.partial, False, type(exc).__name__
    except Exception as exc:
        return b"", False, type(exc).__name__
    finally:
        resp.close()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self): self._proxy()
    def do_GET(self): self._proxy()
    def log_message(self, *_args): pass

    def _proxy(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        agent_id = _actor(self.headers)
        if ACTOR_TOKENS and agent_id is None:
            self.send_error(401, "Unknown local actor credential")
            return
        rid = str(uuid.uuid4())
        context = {"request_id": rid, "agent_id": agent_id or "unattributed",
                   "episode_id": os.environ.get("USAGE_EPISODE_ID", "unknown"),
                   "cohort_id": os.environ.get("USAGE_COHORT_ID", "unknown")}
        def record(event, **fields):
            append_event(USAGE_LOG_PATH, {**context, "event": event, **fields})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                chunks = []
                while True:
                    n = int(self.rfile.readline().strip().split(b";", 1)[0], 16)
                    if n == 0:
                        while self.rfile.readline().strip(): pass
                        break
                    chunks.append(self.rfile.read(n))
                    self.rfile.readline()
                body = b"".join(chunks)
            else:
                body = self.rfile.read(length) if length else b""
        except (ValueError, OSError):
            self.send_error(400, "Invalid request framing")
            return
        model, effort = None, None
        if body and "json" in self.headers.get("Content-Type", "").lower():
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    data.pop("metadata", None)
                    if "/responses" in self.path:
                        inc = data.get("include")
                        if isinstance(inc, list):
                            keep = [x for x in inc if "encrypted_content" not in str(x)]
                            if keep: data["include"] = keep
                            else: data.pop("include", None)
                        items = data.get("input")
                        if isinstance(items, list):
                            data["input"] = [it for it in items if not (isinstance(it, dict) and it.get("type") == "reasoning")]
                        if EXPECTED_EFFORT:
                            data["reasoning"] = {**(data.get("reasoning") or {}), "effort": EXPECTED_EFFORT}
                    elif "/chat/completions" in self.path and EXPECTED_EFFORT:
                        data["reasoning_effort"] = EXPECTED_EFFORT
                    if EXPECTED_MODEL:
                        data["model"] = EXPECTED_MODEL
                    data.update(EXTRA_BODY)
                    model = data.get("model")
                    effort = (data.get("reasoning") or {}).get("effort", data.get("reasoning_effort"))
                    body = json.dumps(data).encode()
            except (ValueError, TypeError, AttributeError):
                self.send_error(400, "Invalid model JSON payload")
                return
        try:
            record("request_started", model=model, reasoning_effort=effort, method=self.command,
                   path=self.path.split("?", 1)[0])
        except OSError:
            self.send_error(503, "Usage ledger unavailable")
            return
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() not in
                   ("host", "authorization", "x-api-key", "api-key", "x-agent-id", "x-actor-id")}
        # Preserve legacy auth forwarding only when attribution/injection is disabled.
        if UPSTREAM_AUTH_TOKEN:
            headers["api-key" if UPSTREAM_AUTH_STYLE == "api-key" else "Authorization"] = (
                UPSTREAM_AUTH_TOKEN if UPSTREAM_AUTH_STYLE == "api-key" else f"Bearer {UPSTREAM_AUTH_TOKEN}")
        elif not ACTOR_TOKENS:
            headers.update({k: v for k, v in self.headers.items() if k.lower() in ("authorization", "x-api-key", "api-key")})
        headers["Content-Length"] = str(len(body))
        attempts = max(1, int(os.environ.get("UPSTREAM_RETRY_ATTEMPTS", "4")))
        timeout = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "600"))
        resp_headers, resp_body, status, error = [], b"", None, None
        for attempt in range(1, attempts + 1):
            record("attempt_started", attempt=attempt)
            started = time.monotonic()
            transport_complete, error = False, None
            resp_body, resp_headers, status = b"", [], None
            try:
                req = urllib.request.Request(UPSTREAM.rstrip("/") + self.path, data=body,
                                             headers=headers, method=self.command)
                try:
                    response = urllib.request.urlopen(req, timeout=timeout)
                except urllib.error.HTTPError as exc:
                    response = exc
                status = response.status
                resp_headers = list(response.headers.items())
                resp_body, transport_complete, error = _read_response(response)
                retry = (status in (429, 500, 502, 503, 504) and transport_complete and attempt < attempts)
            except Exception as exc:
                error = type(exc).__name__
                # A timeout may already have incurred inference; never blindly retry it.
                retry = not _timed_out(exc) and attempt < attempts
            ct = next((v for k, v in resp_headers if k.lower() == "content-type"), "")
            usage = extract_usage(resp_body, ct, transport_complete)
            record("attempt_finished", attempt=attempt, status=status, error=error, retry=retry,
                   duration_s=round(time.monotonic() - started, 4), usage=usage)
            if not retry:
                break
            time.sleep([1, 2, 5, 12][min(attempt - 1, 3)])
        if error is not None:
            record("request_finished", status=status, delivery="upstream_incomplete", error=error)
            print(f"[proxy {rid}] {self.command} {self.path.split('?', 1)[0]} in={len(body)}b -> BAD: {error}", flush=True)
            self.send_error(502, "Upstream response incomplete")
            return
        self.send_response(status)
        for key, value in resp_headers:
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(resp_body)))
        delivery = "delivered"
        try:
            self.end_headers()
            self.wfile.write(resp_body)
            self.wfile.flush()
        except (OSError, ConnectionError):
            delivery = "client_disconnected"
        record("request_finished", status=status, delivery=delivery)
        # Keep the legacy method/path/status shape consumed by relay_stats().
        print(f"[proxy {rid}] {self.command} {self.path.split('?', 1)[0]} in={len(body)}b -> {status} "
              f"out={len(resp_body)}b actor={agent_id or 'unattributed'} {delivery}", flush=True)


def main():
    if not UPSTREAM:
        sys.exit("FATAL: UPSTREAM is not set")
    if USAGE_LOG_PATH and not ACTOR_TOKENS:
        sys.exit("FATAL: usage accounting requires USAGE_ACTOR_TOKENS_JSON")
    if not isinstance(ACTOR_TOKENS, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in ACTOR_TOKENS.items()) or len(set(ACTOR_TOKENS.values())) != len(ACTOR_TOKENS):
        sys.exit("FATAL: actor credentials must be unique nonempty strings")
    if USAGE_LOG_PATH:
        # An empty valid ledger distinguishes a genuine zero-call run from lost evidence.
        fd = os.open(USAGE_LOG_PATH, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
    print(f"strip-metadata proxy listening on {LISTEN_HOST}:{LISTEN_PORT}; accounting={bool(USAGE_LOG_PATH)}", flush=True)
    http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

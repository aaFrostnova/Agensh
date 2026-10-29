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
from troy_replication.journal import request_receipts, response_outcome, emit as troy_emit
from persistent_peer.budget import TeamBudget, unbounded_input_reference

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


def _team_budget():
    path = os.environ.get("TEAM_BUDGET_PATH")
    if not path:
        return None
    return TeamBudget(path, limit=int(os.environ.get("TEAM_BUDGET_LIMIT", "2000000")),
                      actors=json.loads(os.environ.get("TEAM_BUDGET_ACTORS_JSON", '["benign_a","benign_b","benign_c"]')),
                      input_floor=int(os.environ.get("TEAM_BUDGET_RESERVE_INPUT_TOKENS", "65536")),
                      output_ceiling=int(os.environ.get("TEAM_BUDGET_RESERVE_OUTPUT_TOKENS", "8192")),
                      template_allowance=int(os.environ.get("TEAM_BUDGET_TEMPLATE_ALLOWANCE_TOKENS", "16384")))


def _actor_path(agent_id, mapping_env, victim_env):
    mapping = json.loads(os.environ.get(mapping_env, "{}"))
    return mapping.get(agent_id) or (os.environ.get(victim_env) if agent_id == "victim" else None)


def _cap_output(data, path, budget):
    configured = os.environ.get("UPSTREAM_MAX_OUTPUT_TOKENS")
    ceiling = int(configured) if configured else (budget.output_ceiling if budget else None)
    if ceiling is None:
        return
    if budget:
        ceiling = min(ceiling, budget.output_ceiling)
    if ceiling <= 0:
        raise ValueError("invalid output ceiling")
    if data.get("n", 1) != 1:
        raise ValueError("output ceiling requires one completion per request")
    field = "max_output_tokens" if "/responses" in path else (
        "max_completion_tokens" if "max_completion_tokens" in data else "max_tokens")
    asked = data.get(field, ceiling)
    if not isinstance(asked, int) or isinstance(asked, bool) or asked <= 0:
        raise ValueError("invalid output token limit")
    data[field] = min(asked, ceiling)
    if "/chat/completions" in path:
        data.pop("max_tokens" if field == "max_completion_tokens" else "max_completion_tokens", None)


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
        budget = _team_budget()
        budgeted = bool(budget and agent_id in budget.actors and self.command == "POST")
        if budgeted and not any(endpoint in self.path for endpoint in ("/responses", "/chat/completions")):
            self.send_error(400, "Budgeted actors require a supported text model endpoint")
            return
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
        troy_receipts = []
        context_event_path = _actor_path(agent_id, "TROY_ACTOR_CONTEXT_EVENT_PATHS_JSON", "TROY_CONTEXT_EVENT_PATH")
        if budgeted and (not body or "json" not in self.headers.get("Content-Type", "").lower()):
            self.send_error(400, "Budgeted actors require complete text JSON history")
            return
        if body and "json" in self.headers.get("Content-Type", "").lower():
            try:
                data = json.loads(body)
                if budgeted and unbounded_input_reference(data):
                    raise ValueError("unbounded server-side input reference")
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
                    _cap_output(data, self.path, budget)
                    policy_path = _actor_path(agent_id, "TROY_ACTOR_POLICY_FILES_JSON", "TROY_VICTIM_SYSTEM_FILE")
                    if policy_path:
                        policy = Path(policy_path).read_text(encoding="utf-8").strip()
                        if not policy:
                            raise ValueError("empty actor policy")
                        if "/responses" in self.path:
                            original = data.get("instructions") or ""
                            data["instructions"] = policy + ("\n\n" + original if original else "")
                        else:
                            data["messages"] = [{"role": "system", "content": policy}, *data.get("messages", [])]
                    model = data.get("model")
                    effort = (data.get("reasoning") or {}).get("effort", data.get("reasoning_effort"))
                    context_registry = _actor_path(agent_id, "TROY_ACTOR_CONTEXT_REGISTRIES_JSON", "TROY_CONTEXT_REGISTRY")
                    if context_registry:
                        if not context_event_path:
                            raise ValueError("context registry requires event path")
                        troy_receipts = request_receipts(data, context_registry)
                    if policy_path and context_event_path:
                        import hashlib
                        troy_emit(context_event_path, "victim_policy_applied" if agent_id == "victim" else "actor_policy_applied",
                                  request_id=rid, agent_id=agent_id, policy_sha256=hashlib.sha256(policy.encode()).hexdigest(),
                                  delivery="responses_instructions" if "/responses" in self.path else "system_message")
                    if budgeted and unbounded_input_reference(data):
                        raise ValueError("unbounded host-configured server-side input reference")
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
            if budgeted:
                try:
                    reservation = budget.reserve(agent_id, rid, attempt, request_bytes=len(body))
                except (OSError, ValueError):
                    record("request_upstream_finished", status=503, error="budget_ledger_unavailable")
                    record("request_finished", status=503, delivery="budget_ledger_unavailable")
                    self.send_error(503, "Budget ledger unavailable")
                    return
                record("budget_reservation", attempt=attempt, **reservation)
                if not reservation["allowed"]:
                    status = 402
                    record("request_upstream_finished", status=status, error="team_token_budget_exhausted")
                    record("request_finished", status=status, delivery="budget_denied")
                    raw = json.dumps({"error": {"type": "budget_exhausted", "code": "team_token_budget_exhausted",
                                                "message": "The benign team's shared admission budget is exhausted; stop this actor.",
                                                "reason": reservation["reason"], "retryable": False}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    print(f"[proxy {rid}] {self.command} {self.path.split('?', 1)[0]} in={len(body)}b -> 402 "
                          f"out={len(raw)}b actor={agent_id} budget_denied", flush=True)
                    return
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
            if budgeted:
                settlement = budget.settle(agent_id, rid, attempt, usage)
                record("budget_settlement", attempt=attempt, **settlement)
                if settlement["exhausted"]:
                    retry = False
            if not retry:
                break
            time.sleep([1, 2, 5, 12][min(attempt - 1, 3)])
        # Commit the end of the upstream billing transaction before exposing any
        # response bytes. Client delivery finishes later and is a separate fact:
        # a disconnect must not hide already incurred, provider-reported usage.
        record("request_upstream_finished", status=status, error=error)
        if error is not None:
            record("request_finished", status=status, delivery="upstream_incomplete", error=error)
            print(f"[proxy {rid}] {self.command} {self.path.split('?', 1)[0]} in={len(body)}b -> BAD: {error}", flush=True)
            self.send_error(502, "Upstream response incomplete")
            return
        if status is not None and 200 <= status < 300 and troy_receipts:
            for receipt in troy_receipts:
                troy_emit(context_event_path, "message_context_included",
                          request_id=rid, agent_id=agent_id, **receipt)
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
        record("request_finished", status=status, delivery=delivery, **response_outcome(resp_body, ct))
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
    budget = _team_budget()
    if budget:
        if not USAGE_LOG_PATH or not set(budget.actors).issubset(ACTOR_TOKENS):
            sys.exit("FATAL: budget requires usage accounting and credentials for every budgeted actor")
        budget.snapshot()
    for env_name in ("TROY_ACTOR_POLICY_FILES_JSON", "TROY_ACTOR_CONTEXT_REGISTRIES_JSON", "TROY_ACTOR_CONTEXT_EVENT_PATHS_JSON"):
        mapping = json.loads(os.environ.get(env_name, "{}"))
        if not isinstance(mapping, dict) or any(actor not in ACTOR_TOKENS or not isinstance(path, str) or not path
                                               for actor, path in mapping.items()):
            sys.exit(f"FATAL: {env_name} must map authenticated actors to nonempty paths")
    if USAGE_LOG_PATH:
        # An empty valid ledger distinguishes a genuine zero-call run from lost evidence.
        fd = os.open(USAGE_LOG_PATH, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
    print(f"strip-metadata proxy listening on {LISTEN_HOST}:{LISTEN_PORT}; accounting={bool(USAGE_LOG_PATH)}", flush=True)
    http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

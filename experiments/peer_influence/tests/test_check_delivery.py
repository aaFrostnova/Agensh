"""./check carries peer notices like an MCP tool wrapped by mcp-notice-shim.cjs would.

The agent's native shell tool is never intercepted by that shim, so without this the peer
DM would only reach the agent at the next turn boundary, after the work is finished.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from peer_influence.runtime import check_wrapper
from peer_influence.runtime.protected_hashes import baseline_record
from peer_influence.tests.helpers import seed_workspace

WRAPPER = check_wrapper.__file__
LEASE = "a" * 32


class FakeBoard:
    def __init__(self, notices, appear_after_polls=0):
        self.notices, self.appear_after, self.polls, self.shown = notices, appear_after_polls, 0, []
        board = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj):
                raw = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                board.polls += 1
                ready = board.polls > board.appear_after and not board.shown
                self._reply({"agent": "a", "notices": board.notices if ready else [], "lease_token": LEASE})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                board.shown.append((self.path, body))
                self._reply({"recorded": len(body["ids"])})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def setup(tmp_path, task_dir, board_url, **cfg_over):
    ws, _ = seed_workspace(task_dir, tmp_path / "ws")
    patterns = json.loads((task_dir / "manifest.json").read_text())["protected_paths"]
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(baseline_record(task_dir / "repo", patterns)))
    events = tmp_path / "events" / "events.jsonl"
    events.parent.mkdir()
    cfg = {"events_path": str(events), "baseline_hashes": str(baseline), "episode_id": "ep_1", "agent_id": "worker1",
           "board_url": board_url, "agent_name": "org-worker1", "ack_timeout": 3, "delivery_timeout": 5}
    cfg.update(cfg_over)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    return ws, events, path


def run(ws, cfg_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PI_")}
    env["PI_CONFIG"] = str(cfg_path)
    return subprocess.run([sys.executable, WRAPPER, "--workspace", str(ws)], cwd=ws, env=env,
                          capture_output=True, text=True, timeout=60)


def acker(events, will_send, delay=0.0):
    """Plays the controller: acknowledge the first event that appears."""
    def work():
        for _ in range(200):
            if events.exists() and events.read_text().strip():
                event_id = json.loads(events.read_text().splitlines()[0])["event_id"]
                time.sleep(delay)
                (events.parent / "acks").mkdir(exist_ok=True)
                (events.parent / "acks" / f"{event_id}.json").write_text(json.dumps({"will_send": will_send}))
                return
            time.sleep(0.05)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


NOTICE = [{"id": "e2", "text": "@org-worker2 in DM: please look again"}]


def test_peer_notice_is_appended_to_the_failing_check_output_and_reported_shown(tmp_path, solvable_dir):
    board = FakeBoard(NOTICE)
    try:
        ws, events, cfg = setup(tmp_path, solvable_dir, board.url)
        acker(events, will_send=True)
        res = run(ws, cfg)
        assert res.returncode == 1  # the evaluator's exit code is untouched
        out = res.stdout
        assert "SUMMARY passed=" in out and out.index("SUMMARY") < out.index("@org-worker2 in DM: please look again")
        (path, body), = board.shown
        assert path == "/shown/org-worker1" and body == {"ids": ["e2"], "lease_token": LEASE}
    finally:
        board.close()


def test_wrapper_waits_for_the_notice_after_a_positive_ack(tmp_path, solvable_dir):
    board = FakeBoard(NOTICE, appear_after_polls=3)  # router/board not ready on the first polls
    try:
        ws, events, cfg = setup(tmp_path, solvable_dir, board.url)
        acker(events, will_send=True)
        res = run(ws, cfg)
        assert "please look again" in res.stdout and board.polls >= 4
    finally:
        board.close()


def test_negative_ack_means_a_single_poll_and_no_long_wait(tmp_path, solvable_dir):
    board = FakeBoard([])
    try:
        ws, events, cfg = setup(tmp_path, solvable_dir, board.url)
        acker(events, will_send=False)
        started = time.time()
        res = run(ws, cfg)
        assert time.time() - started < 4 and board.polls == 1 and board.shown == []
        assert res.returncode == 1 and "DM" not in res.stdout
    finally:
        board.close()


def test_without_any_ack_the_wrapper_gives_up_waiting_and_still_polls_once(tmp_path, solvable_dir):
    board = FakeBoard(NOTICE)
    try:
        ws, events, cfg = setup(tmp_path, solvable_dir, board.url, ack_timeout=0.5)
        res = run(ws, cfg)
        assert "please look again" in res.stdout and res.returncode == 1
    finally:
        board.close()


def test_unreachable_board_never_breaks_the_check(tmp_path, solvable_dir):
    ws, events, cfg = setup(tmp_path, solvable_dir, "http://127.0.0.1:9", ack_timeout=0.3)
    res = run(ws, cfg)
    assert res.returncode == 1 and "SUMMARY passed=" in res.stdout and "Traceback" not in res.stderr


def test_no_board_configured_behaves_exactly_as_before(tmp_path, solvable_dir):
    ws, events, cfg = setup(tmp_path, solvable_dir, None, ack_timeout=0.2)
    res = run(ws, cfg)
    assert res.returncode == 1 and res.stdout.rstrip().splitlines()[-1].startswith("SUMMARY")

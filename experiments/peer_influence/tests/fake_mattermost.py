"""A tiny in-process Mattermost REST fake (test-only). Real HTTP, real client code under test."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeMattermost:
    def __init__(self):
        self.users = {
            "u_admin": {"id": "u_admin", "username": "mmadmin"},
            "u_w1": {"id": "u_w1", "username": "org-worker1"},
            "u_w2": {"id": "u_w2", "username": "org-worker2"},
        }
        self.tokens = {"tok_admin": "u_admin", "tok_w1": "u_w1", "tok_w2": "u_w2"}
        self.posts = []
        self.channels = {}
        self.fail_next_posts = 0  # respond 500 without creating the post
        self.fail_after_create_next = 0  # create the post but respond 500 (lost response)
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = "http://127.0.0.1:%d" % self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _user(self):
                auth = self.headers.get("Authorization", "")
                return fake.tokens.get(auth.removeprefix("Bearer "))

            def _body(self):
                n = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                uid = self._user()
                if uid is None:
                    return self._reply(401, {"message": "unauthorized"})
                url = urlparse(self.path)
                parts = url.path.strip("/").split("/")
                with fake._lock:
                    if parts[-2:] == ["users", "me"]:
                        return self._reply(200, fake.users[uid])
                    if parts[-3:-1] == ["users", "username"]:
                        match = [u for u in fake.users.values() if u["username"] == parts[-1]]
                        return self._reply(200, match[0]) if match else self._reply(404, {})
                    if parts[-3] == "channels" and parts[-1] == "posts":
                        posts = [p for p in fake.posts if p["channel_id"] == parts[-2]]
                        posts.sort(key=lambda p: p["create_at"], reverse=True)
                        return self._reply(200, {"order": [p["id"] for p in posts], "posts": {p["id"]: p for p in posts}})
                    if parts[-2] == "posts":
                        match = [p for p in fake.posts if p["id"] == parts[-1]]
                        return self._reply(200, match[0]) if match else self._reply(404, {})
                self._reply(404, {})

            def do_POST(self):
                uid = self._user()
                if uid is None:
                    return self._reply(401, {"message": "unauthorized"})
                path = urlparse(self.path).path
                body = self._body()
                with fake._lock:
                    if path.endswith("/channels/direct"):
                        cid = "dm_" + "_".join(sorted(body))
                        fake.channels[cid] = {"id": cid, "type": "D", "members": sorted(body)}
                        return self._reply(200, fake.channels[cid])
                    if path.endswith("/posts"):
                        if fake.fail_next_posts:
                            fake.fail_next_posts -= 1
                            return self._reply(500, {"message": "boom"})
                        post = {
                            "id": "post%03d" % (len(fake.posts) + 1), "user_id": uid,
                            "channel_id": body["channel_id"], "message": body["message"],
                            "props": body.get("props") or {}, "create_at": len(fake.posts) + 1,
                        }
                        fake.posts.append(post)
                        if fake.fail_after_create_next:
                            fake.fail_after_create_next -= 1
                            return self._reply(500, {"message": "response lost"})
                        return self._reply(201, post)
                self._reply(404, {})

        return Handler

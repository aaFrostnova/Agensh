"""Minimal Mattermost REST client for the scripted peer identity."""
from __future__ import annotations

import requests


class MattermostError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class MattermostClient:
    def __init__(self, base_url, token, timeout=15.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method, path, **kwargs):
        try:
            resp = requests.request(
                method, f"{self.base}/api/v4{path}", timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.token}"}, **kwargs,
            )
        except requests.RequestException as exc:
            raise MattermostError(f"{method} {path}: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise MattermostError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
        return resp.json()

    def me(self):
        return self._request("GET", "/users/me")

    def user_by_username(self, username):
        return self._request("GET", f"/users/username/{username}")

    def ensure_dm(self, user_id_a, user_id_b):
        return self._request("POST", "/channels/direct", json=[user_id_a, user_id_b])

    def create_post(self, channel_id, message, props):
        return self._request("POST", "/posts", json={"channel_id": channel_id, "message": message, "props": props})

    def get_post(self, post_id):
        return self._request("GET", f"/posts/{post_id}")

    def recent_posts(self, channel_id, per_page=60):
        page = self._request("GET", f"/channels/{channel_id}/posts?page=0&per_page={per_page}")
        return [page["posts"][pid] for pid in page.get("order", [])]

    def find_post_by_key(self, channel_id, key, author_id):
        """The post this experiment already made for `key`, if any (crash/lost-response recovery)."""
        for post in self.recent_posts(channel_id):
            if post.get("user_id") == author_id and (post.get("props") or {}).get("idempotency_key") == key:
                return post
        return None

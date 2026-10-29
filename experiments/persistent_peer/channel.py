"""A fixed-identity client for one shared Mattermost experiment channel.

The peer cannot select another channel, impersonate a user, DM someone, or
upload a file. Transport success is recorded separately from model exposure;
the authenticated relay provides the latter from actual inference inputs.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import threading
import uuid
from typing import Any

import requests

from troy_replication.journal import emit, register_message

MAX_MESSAGE_CHARS = 8192


class SharedChannel:
    def __init__(self, *, mattermost_url: str, token: str, username: str,
                 channel_id: str, run_dir: str | Path, registry_path: str | Path,
                 event_path: str | Path, allowed_mentions: dict[str, str] | None = None):
        if not channel_id or not username:
            raise ValueError("fixed channel and username are required")
        self.mattermost_url = mattermost_url.rstrip("/")
        self.username, self.channel_id = username, channel_id
        self.run_dir = Path(run_dir)
        self.registry_path, self.event_path = Path(registry_path), Path(event_path)
        self.allowed_mentions = dict(allowed_mentions or {})
        self._http = requests.Session()
        self._http.headers.update({"Authorization": "Bearer " + token})
        self._user_id: str | None = None
        self._users: dict[str, str] = {}
        self._lock = threading.RLock()
        self._registered_posts: dict[tuple[str, str], str] = {}

    @property
    def user_id(self) -> str:
        self._ensure_identity()
        assert self._user_id is not None
        return self._user_id

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        response = self._http.request(method, self.mattermost_url + "/api/v4" + path,
                                      timeout=(5, 20), **kwargs)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"mattermost_http_{response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("mattermost_invalid_response")
        return body

    def _ensure_identity(self) -> None:
        with self._lock:
            if self._user_id is not None:
                return
            own = self._request("GET", "/users/me")
            if own.get("username") != self.username or not own.get("id"):
                raise RuntimeError("mattermost_peer_identity_mismatch")
            channel = self._request("GET", "/channels/" + self.channel_id)
            if channel.get("id") != self.channel_id or channel.get("type") not in ("O", "P"):
                raise RuntimeError("mattermost_expected_shared_channel")
            member = self._request("GET", f"/channels/{self.channel_id}/members/{own['id']}")
            if member.get("user_id") != own["id"] or member.get("channel_id") != self.channel_id:
                raise RuntimeError("mattermost_peer_not_channel_member")
            self._user_id = str(own["id"])
            self._users[self._user_id] = self.username

    def _username(self, user_id: str) -> str:
        if user_id not in self._users:
            user = self._request("GET", "/users/" + user_id)
            if user.get("id") != user_id or not isinstance(user.get("username"), str):
                raise RuntimeError("mattermost_invalid_author")
            self._users[user_id] = user["username"]
        return self._users[user_id]

    def read_messages(self) -> list[dict[str, Any]]:
        """Read public text only, including replies; never fetch attachments.

        Pagination prevents silently dropping a burst. A fresh per-episode
        channel is expected. An unexpected >10,000-post channel fails visibly.
        """
        with self._lock:
            return self._read_messages_locked()

    def _read_messages_locked(self) -> list[dict[str, Any]]:
        self._ensure_identity()
        posts: dict[str, dict[str, Any]] = {}
        for page in range(100):
            body = self._request("GET", f"/channels/{self.channel_id}/posts",
                                 params={"page": page, "per_page": 100})
            rows, order = body.get("posts"), body.get("order")
            if not isinstance(rows, dict) or not isinstance(order, list):
                raise RuntimeError("mattermost_invalid_posts")
            for post_id in order:
                row = rows.get(post_id)
                if not isinstance(row, dict) or row.get("id") != post_id:
                    raise RuntimeError("mattermost_invalid_post")
                if row.get("channel_id") != self.channel_id:
                    raise RuntimeError("mattermost_cross_channel_post")
                if row.get("delete_at") or str(row.get("type", "")).startswith("system_"):
                    continue
                text = row.get("message")
                if not isinstance(text, str) or not text.strip():
                    continue
                author_id = str(row.get("user_id", ""))
                if not author_id:
                    raise RuntimeError("mattermost_post_missing_author")
                digest = hashlib.sha256(text.encode()).hexdigest()
                version = (str(post_id), digest)
                if version not in self._registered_posts:
                    message_id = "channel:" + str(post_id) + ":" + digest[:12]
                    register_message(self.registry_path, message_id, text, social=True, post_id=post_id)
                    self._registered_posts[version] = message_id
                    emit(self.event_path, "channel_post_observed", actor=self._username(author_id),
                         message_id=message_id, post_id=post_id, channel_id=self.channel_id,
                         text=text, text_sha256=digest, author_id=author_id)
                posts[post_id] = {
                    "key": "channel-post:" + str(post_id) + ":" + digest,
                    "message_id": self._registered_posts[version], "post_id": str(post_id),
                    "channel_id": self.channel_id, "author_id": author_id,
                    "author": self._username(author_id), "text": text,
                    "created_at_ms": row.get("create_at", 0),
                    "updated_at_ms": row.get("update_at", row.get("create_at", 0)),
                    "root_id": row.get("root_id", ""),
                }
            if len(order) < 100:
                break
        else:
            raise RuntimeError("mattermost_channel_history_exceeds_safety_bound")
        return sorted(posts.values(), key=lambda row: (row["created_at_ms"], row["post_id"]))

    def send_message(self, message: str, *, targets: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            return self._send_message_locked(message, targets=targets)

    def _send_message_locked(self, message: str, *, targets: list[str] | None = None) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a nonempty string")
        targets = [] if targets is None else targets
        if (not isinstance(targets, list) or any(not isinstance(x, str) for x in targets)
                or len(set(targets)) != len(targets)
                or any(x not in self.allowed_mentions for x in targets)):
            raise ValueError("targets must be distinct configured team aliases")
        prefix = " ".join("@" + self.allowed_mentions[x] for x in targets)
        text = (prefix + "\n" if prefix else "") + message
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message including mentions exceeds {MAX_MESSAGE_CHARS} characters")
        self._ensure_identity()
        message_id = "persistent-peer-" + uuid.uuid4().hex
        register_message(self.registry_path, message_id, text, social=True)
        emit(self.event_path, "message_registered", message_id=message_id,
             actor="adversary", channel_id=self.channel_id, social=True)
        # Never automatically retry a POST: a lost response may still mean that
        # Mattermost committed it. Registry intent is deliberately retained.
        try:
            post = self._request("POST", "/posts", json={"channel_id": self.channel_id, "message": text})
            if (not isinstance(post.get("id"), str) or not post["id"]
                    or post.get("channel_id") != self.channel_id
                    or post.get("user_id") != self._user_id
                    or post.get("message") != text):
                raise RuntimeError("mattermost_post_identity_or_content_mismatch")
        except Exception as exc:
            emit(self.event_path, "message_delivery_failed", actor="adversary",
                 message_id=message_id, reason="send_failed_or_ambiguous:" + type(exc).__name__,
                 retry_performed=False)
            raise
        register_message(self.registry_path, message_id, text, social=True, post_id=post["id"])
        self._registered_posts[(post["id"], hashlib.sha256(text.encode()).hexdigest())] = message_id
        result = {"sent": True, "message_id": message_id, "post_id": post["id"],
                  "channel_id": self.channel_id, "text": text, "targets": targets,
                  "visibility": "shared_channel_all_members"}
        emit(self.event_path, "message_sent", actor="adversary", social=True, **result)
        return result

    def close(self) -> None:
        self._http.close()

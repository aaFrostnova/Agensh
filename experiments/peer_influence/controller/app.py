#!/usr/bin/env python3
"""Treatment controller: send one scripted peer DM after the victim's first failed check.

State machine (plan 10.1):
CREATED -> WAITING_FOR_FIRST_FAILED_CHECK -> SENDING -> TREATMENT_SENT -> EPISODE_DONE
(solo: ... -> TRIGGERED_NO_TREATMENT; failures: FAILED_IDENTITY, FAILED_SEND).
The state file is written before every send and after every outcome. A restart in
SENDING first looks for a post carrying this episode's idempotency key, so a lost
response or a crash can never produce a second message.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from peer_influence.controller.mattermost import MattermostClient, MattermostError  # noqa: E402
from peer_influence.controller.monitor import WorkspaceMonitor  # noqa: E402
from peer_influence.controller.schemas import ControllerConfig  # noqa: E402,F401
from peer_influence.common import write_json_atomic  # noqa: E402
from peer_influence.controller.state import EventTail, StateStore  # noqa: E402
from peer_influence.runtime.emit_event import append_event  # noqa: E402


SAFE_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TreatmentController:
    def __init__(self, cfg, mm=None, clock=time.time):
        self.cfg = cfg
        self.clock = clock
        self.mm = mm or MattermostClient(cfg.mattermost_url, cfg.peer_token)
        cfg.run_dir.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(cfg.run_dir / "controller_state.json", {
            "state": "CREATED", "key": cfg.key, "treatment_type": cfg.treatment_type,
            "message_sha256": cfg.message_sha256, "trigger_source": None, "triggered_at": None,
            "started_at": None, "events_offset": 0, "malformed_events": 0, "check_events_seen": 0,
            "send_attempts": 0, "last_error": None, "sent": None,
        })
        self.state = self.store.data
        self.tail = EventTail(cfg.events_path, self.state["events_offset"])
        self._acks = []  # (event_id, is_trigger) waiting to be acknowledged to ./check
        self.monitor = None
        if cfg.workspace is not None and cfg.baseline_hashes is not None:
            self.monitor = WorkspaceMonitor(
                cfg.workspace, cfg.protected_patterns, cfg.baseline_hashes,
                cfg.run_dir / "workspace_events.jsonl", clock=clock)

    def step(self):
        """One polling iteration; safe to call at any rate and after any restart."""
        s = self.state
        if s["state"] == "EPISODE_DONE":
            return s["state"]
        if self.monitor is not None:
            self.monitor.poll()
        if s["state"] == "CREATED":
            s.update(state="WAITING_FOR_FIRST_FAILED_CHECK", started_at=self.clock())
            self.store.save()
        if s["state"] == "WAITING_FOR_FIRST_FAILED_CHECK":
            self._look_for_trigger()
        if s["state"] == "SENDING":
            self._send()
        self._flush_acks()
        return s["state"]

    def _look_for_trigger(self):
        s = self.state
        events, malformed = self.tail.read_new()
        s["malformed_events"] += malformed
        trigger = None
        for ev in events:
            if ev.get("event_type") != "check_result":
                continue
            s["check_events_seen"] += 1
            is_trigger = trigger is None and ev.get("exit_code") not in (0, None)
            if is_trigger:
                trigger = "first_failed_check"
            event_id = ev.get("event_id")
            if isinstance(event_id, str) and SAFE_EVENT_ID.match(event_id):  # the log is writable by the agent
                self._acks.append((event_id, is_trigger))
        if trigger is None and self.clock() - s["started_at"] >= self.cfg.fallback_after_seconds:
            trigger = "fallback_timer"
        s["events_offset"] = self.tail.offset
        if trigger is None:
            if events or malformed:
                self.store.save()
            return
        s.update(trigger_source=trigger, triggered_at=self.clock(),
                 state="TRIGGERED_NO_TREATMENT" if self.cfg.treatment_type == "none" else "SENDING")
        self.store.save()  # write-ahead: SENDING is durable before any message leaves

    def _send(self):
        s, cfg = self.state, self.cfg
        try:
            me = self.mm.me()
            if me["username"] != cfg.peer_username:
                s.update(state="FAILED_IDENTITY", last_error=f"token belongs to {me['username']!r}, not the peer")
                self.store.save()
                return
            victim = self.mm.user_by_username(cfg.victim_username)
            channel = self.mm.ensure_dm(me["id"], victim["id"])
            post = self.mm.find_post_by_key(channel["id"], cfg.key, me["id"]) or self.mm.create_post(
                channel["id"], cfg.message, {"idempotency_key": cfg.key})
            if post["user_id"] != me["id"] or post["channel_id"] != channel["id"]:
                s.update(state="FAILED_IDENTITY", last_error="created post has the wrong author or channel")
                self.store.save()
                return
        except MattermostError as exc:
            s["send_attempts"] += 1
            s["last_error"] = str(exc)
            if s["send_attempts"] >= cfg.max_send_attempts:
                s["state"] = "FAILED_SEND"
            self.store.save()
            return
        record = {
            "post_id": post["id"], "channel_id": channel["id"], "sent_at": self.clock(),
            "author_id": me["id"], "author_username": me["username"],
            "recipient_username": cfg.victim_username, "message_sha256": cfg.message_sha256,
            "idempotency_key": cfg.key, "episode_id": cfg.episode_id,
        }
        self._log_message(record)
        s.update(state="TREATMENT_SENT", sent=record, last_error=None)
        self.store.save()

    def _flush_acks(self):
        """Tell ./check whether a peer message is on its way, so it can carry it (see runtime/check_wrapper)."""
        keep = []
        for event_id, is_trigger in self._acks:
            if is_trigger and self.state["state"] == "SENDING":
                keep.append((event_id, is_trigger))  # the DM is not out yet
                continue
            body = {"will_send": False}
            sent = self.state.get("sent")
            if is_trigger and self.state["state"] == "TREATMENT_SENT" and sent:
                body = {"will_send": True, "post_id": sent["post_id"]}
            write_json_atomic(self.cfg.events_path.parent / "acks" / f"{event_id}.json", body)
        self._acks = keep

    def _log_message(self, record):
        path = self.cfg.run_dir / "messages.jsonl"
        if path.exists() and any(json.loads(x).get("post_id") == record["post_id"]
                                 for x in path.read_text().splitlines() if x.strip()):
            return
        append_event(path, record)

    def finish(self):
        self.state["state"] = "EPISODE_DONE"
        self.store.save()

    def run(self, should_stop):
        while self.state["state"] != "EPISODE_DONE":
            self.step()
            if should_stop():
                break
            time.sleep(self.cfg.poll_interval)
        self.step()
        self.finish()


def main(argv=None):
    import os

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True, help="JSON ControllerConfig without the token")
    args = ap.parse_args(argv)
    raw = json.loads(args.config.read_text(encoding="utf-8"))
    raw["peer_token"] = os.environ["PI_PEER_TOKEN"]
    cfg = ControllerConfig(**raw)
    stop = cfg.run_dir / "STOP"
    TreatmentController(cfg).run(stop.exists)
    return 0


if __name__ == "__main__":
    sys.exit(main())

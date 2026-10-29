"""Host-owned, process-safe shared input-plus-output token reservation ledger.

Only authenticated team actors enter this budget. Cached input is still input;
reasoning is already output. Reservations are conservative admission accounting,
not a tokenizer claim or a mathematical bound on an undocumented provider template.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile


RESERVATION_METHOD = "max(input_floor, serialized_request_utf8_bytes + template_allowance) + output_ceiling"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def unbounded_input_reference(data):
    """Reject server-side history/media expansion that byte reservation cannot bound."""
    if not isinstance(data, dict):
        return "payload_not_object"
    for key in ("previous_response_id", "conversation", "prompt"):
        if data.get(key):
            return key
    for tool in data.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") not in ("function", "custom"):
            return "server_side_tool"
    if isinstance(data.get("input"), list):
        if any(isinstance(item, dict) and set(item) == {"id"} for item in data["input"]):
            return "opaque_input_item_reference"
    forbidden_keys = {"file_id", "file_url", "image_url", "audio_url", "video_url", "vector_store_ids"}
    forbidden_types = {"input_file", "input_image", "input_audio", "image_url", "audio", "video", "item_reference",
                       "file_search", "web_search", "web_search_preview", "computer_use_preview", "code_interpreter"}
    def walk(value):
        if isinstance(value, dict):
            for key in forbidden_keys:
                if value.get(key):
                    return key
            if isinstance(value.get("type"), str) and value["type"] in forbidden_types:
                return str(value["type"])
            for key, child in value.items():
                # JSON Schemas describe literal tool parameters / output shapes;
                # their property names and union types are not provider inputs.
                if key in ("parameters", "input_schema", "schema"):
                    continue
                result = walk(child)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = walk(child)
                if result:
                    return result
        return None
    return walk(data)


class TeamBudget:
    """Atomic shared budget; an interrupted or unknown attempt retains its reservation."""
    def __init__(self, path, *, limit=2_000_000, actors=("benign_a", "benign_b", "benign_c"),
                 input_floor=65536, output_ceiling=8192, template_allowance=16384):
        self.path = Path(path)
        self.actors = tuple(sorted(set(actors)))
        self.limit = int(limit)
        self.input_floor = int(input_floor)
        self.output_ceiling = int(output_ceiling)
        self.template_allowance = int(template_allowance)
        if not self.actors or any(not isinstance(actor, str) or not actor for actor in self.actors):
            raise ValueError("budget requires named authenticated actors")
        if min(self.limit, self.input_floor, self.output_ceiling) <= 0 or self.template_allowance < 0:
            raise ValueError("invalid token budget configuration")

    def _configuration(self):
        return {"limit_tokens": self.limit, "budgeted_actors": list(self.actors),
                "input_floor_tokens": self.input_floor, "output_ceiling_tokens": self.output_ceiling,
                "template_allowance_tokens": self.template_allowance,
                "reservation_method": RESERVATION_METHOD}

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self.path.exists():
                state = json.loads(self.path.read_text(encoding="utf-8"))
                for key, value in self._configuration().items():
                    if state.get(key) != value:
                        raise ValueError(f"budget configuration mismatch: {key}")
            else:
                state = {"schema_version": 1, **self._configuration(), "created_at": _now(),
                         "exhausted": False, "stop_reason": None, "integrity_error": None,
                         "entries": {}, "denied_attempts": [],
                         "semantics": "Provider input plus output, cache included and reasoning not added again. Unknown/in-flight usage retains its conservative reservation. UTF-8 byte reservation assumes provider framing fits the configured template allowance; it is not a proof of a provider token bound."}
            yield state
            self._totals(state)
            state["updated_at"] = _now()
            tmpfd, tmpname = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
            try:
                with os.fdopen(tmpfd, "w", encoding="utf-8") as handle:
                    json.dump(state, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmpname, self.path)
                directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(tmpname):
                    os.unlink(tmpname)
        finally:
            os.close(fd)

    @staticmethod
    def _totals(state):
        entries = list(state["entries"].values())
        known = sum(entry.get("known_input_output_tokens", 0) for entry in entries)
        committed = sum(entry["charged_tokens"] for entry in entries)
        state.update(known_input_output_tokens=known, committed_tokens=committed,
                     remaining_admission_tokens=max(0, state["limit_tokens"] - committed),
                     inflight_reserved_tokens=sum(entry["charged_tokens"] for entry in entries if entry["status"] == "reserved"),
                     unknown_reserved_tokens=sum(entry["charged_tokens"] for entry in entries if entry["status"] == "usage_unknown"),
                     unknown_usage_attempts=sum(entry["status"] == "usage_unknown" for entry in entries),
                     inflight_attempts=sum(entry["status"] == "reserved" for entry in entries),
                     settled_attempts=sum(entry["status"] != "reserved" for entry in entries))

    def snapshot(self):
        with self._locked() as state:
            self._totals(state)
            return json.loads(json.dumps(state))

    def reserve(self, actor, request_id, attempt, *, request_bytes=0):
        if actor not in self.actors:
            return {"allowed": True, "budgeted": False, "reservation_tokens": 0}
        if not isinstance(request_bytes, int) or request_bytes < 0:
            raise ValueError("request_bytes must be nonnegative")
        key = f"{request_id}:{attempt}"
        reservation = max(self.input_floor, request_bytes + self.template_allowance) + self.output_ceiling
        with self._locked() as state:
            self._totals(state)
            if key in state["entries"]:
                raise ValueError("attempt already reserved")
            reason = state["stop_reason"] if state["exhausted"] else (
                "reservation_exhausted" if state["committed_tokens"] + reservation > self.limit else None)
            if reason:
                state.update(exhausted=True, stop_reason=reason)
                state["denied_attempts"].append({"agent_id": actor, "request_id": request_id,
                                                "attempt": attempt, "reason": reason,
                                                "requested_reservation_tokens": reservation, "timestamp": _now()})
                state["denied_attempts"] = state["denied_attempts"][-64:]
                return {"allowed": False, "budgeted": True, "reason": reason,
                        "reservation_tokens": reservation, "remaining_admission_tokens": state["remaining_admission_tokens"]}
            state["entries"][key] = {"agent_id": actor, "request_id": request_id, "attempt": attempt,
                                      "request_utf8_bytes": request_bytes, "reservation_tokens": reservation,
                                      "charged_tokens": reservation, "known_input_output_tokens": 0,
                                      "status": "reserved", "reserved_at": _now()}
            return {"allowed": True, "budgeted": True, "reservation_tokens": reservation,
                    "reservation_method": RESERVATION_METHOD}

    def settle(self, actor, request_id, attempt, usage):
        if actor not in self.actors:
            return {"budgeted": False}
        key = f"{request_id}:{attempt}"
        usage = usage if isinstance(usage, dict) else {}
        inp, out = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
        known = (inp or 0) + (out or 0)
        complete = bool(usage.get("accounting_complete") and inp is not None and out is not None)
        with self._locked() as state:
            entry = state["entries"].get(key)
            if entry is None or entry["agent_id"] != actor:
                raise ValueError("unknown or mismatched budget reservation")
            if entry["status"] != "reserved":
                raise ValueError("attempt already settled")
            entry.update(status="settled" if complete else "usage_unknown", known_input_output_tokens=known,
                         input_tokens=inp, output_tokens=out, accounting_complete=complete, settled_at=_now(),
                         charged_tokens=known if complete else max(entry["reservation_tokens"], known))
            if known > entry["reservation_tokens"]:
                state.update(exhausted=True, stop_reason="budget_integrity_error",
                             integrity_error={"request_id": request_id, "attempt": attempt,
                                              "reservation_tokens": entry["reservation_tokens"],
                                              "known_input_output_tokens": known,
                                              "reason": "provider_usage_exceeded_reservation"})
            self._totals(state)
            if state["committed_tokens"] > self.limit:
                state.update(exhausted=True, stop_reason="budget_integrity_error")
            return {"budgeted": True, "accounting_complete": complete,
                    "charged_tokens": entry["charged_tokens"], "exhausted": state["exhausted"],
                    "stop_reason": state["stop_reason"], "integrity_error": state["integrity_error"]}

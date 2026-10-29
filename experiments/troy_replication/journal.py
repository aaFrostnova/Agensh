"""Host-only journals and evidence of messages in actual inference requests."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

_LOCK = threading.RLock()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def emit(path, event, **fields):
    record = {"schema_version": 1, "event_id": uuid.uuid4().hex, "t": time.time(), "event": event, **fields}
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError("incomplete journal write")
    finally:
        os.close(fd)
    return record


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    # A concurrently appended final partial line is retried on the next read.
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        data = data[:data.rfind(b"\n") + 1]
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def register_message(path, message_id, text, *, social=True, post_id=None):
    """Register before transport. Match full text; post ID alone is weaker evidence."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("message must be nonempty")
    with _LOCK:
        registry = json.loads(Path(path).read_text()) if Path(path).exists() else []
        entry = {"message_id": message_id, "text": text,
                 "sha256": hashlib.sha256(text.encode()).hexdigest(), "social": social, "post_id": post_id}
        existing = next((row for row in registry if row["message_id"] == message_id), None)
        if existing is not None:
            if existing["sha256"] != entry["sha256"]:
                raise ValueError("message identity cannot change content")
            existing.update(entry)
        else:
            registry.append(entry)
        write_json(path, registry)
    return entry


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        if value.get("type") in ("reasoning", "thinking", "redacted_thinking"):
            return
        for key, item in value.items():
            if key not in ("reasoning", "reasoning_content", "thinking", "encrypted_content"):
                yield from _strings(item)


def request_receipts(data, registry_path):
    """Only examine actual input/messages, never model output or reasoning."""
    if not registry_path:
        return []
    rows = json.loads(Path(registry_path).read_text())
    strings = list(_strings(data.get("input", data.get("messages", []))))
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    matches = []
    for row in rows:
        if any(row["text"] in item for item in strings):
            matches.append({key: row.get(key) for key in ("message_id", "post_id", "sha256", "social")}
                           | {"request_sha256": digest, "evidence": "exact_text_in_model_input"})
    return matches


def response_outcome(body, content_type):
    """Extract only terminal provider status; never persist reasoning content."""
    if 'text/event-stream' in content_type:
        objects=[]
        for line in body.decode(errors='replace').splitlines():
            if line.startswith('data:'):
                try: objects.append(json.loads(line[5:].strip()))
                except (ValueError,TypeError): pass
        responses=[obj.get('response') for obj in objects if isinstance(obj,dict) and
                   obj.get('type') in ('response.completed','response.incomplete','response.failed')]
        value=responses[-1] if responses else {}
    else:
        try: value=json.loads(body)
        except (ValueError,TypeError): value={}
    if not isinstance(value,dict): value={}
    details=value.get('incomplete_details') or {}
    return {'response_status':value.get('status'),
            'incomplete_reason':details.get('reason') if isinstance(details,dict) else None}

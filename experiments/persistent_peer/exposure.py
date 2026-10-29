"""Independent actual-inference exposure receipts for the four-agent pilot.

Posting, router delivery, and visible peer text are not sufficient. A receipt
must bind registered exact text to a successful upstream request by that actor.
Absent receipts mean unverified exposure, not evidence that a peer refused it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from troy_replication.journal import read_jsonl


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def summarize_exposure(run, benign_actors=("benign_a", "benign_b", "benign_c")):
    run = Path(run)
    events = read_jsonl(run / "events.jsonl")
    usage = read_jsonl(run / "usage_events.jsonl")
    registry_path = run / "context_registry.json"
    registry = json.loads(registry_path.read_text()) if registry_path.is_file() else []
    registered, alias_to_ids = {}, {}
    for row in registry:
        message_id, text = row.get("message_id"), row.get("text")
        if not isinstance(message_id, str) or not isinstance(text, str):
            continue
        digest = _digest(text)
        if row.get("sha256") != digest:
            continue
        registered[message_id] = {**row, "sha256": digest}
        for key in (message_id, row.get("post_id")):
            if isinstance(key, str) and key:
                alias_to_ids.setdefault(key, set()).add(message_id)

    sent = {}
    for row in events:
        kind = row.get("event", row.get("kind"))
        actor = row.get("actor", row.get("agent_id"))
        is_peer = kind == "peer_message_sent" and actor in (None, "adversary")
        is_sent = kind == "message_sent" and actor == "adversary"
        if not (is_peer or is_sent) or row.get("social") is False:
            continue
        message_id = row.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            continue
        sent[message_id] = {"message_id": message_id, "post_id": row.get("post_id"),
                            "event_id": row.get("event_id"), "text": row.get("text")}

    # Only durable upstream completion proves that inference accepted the input.
    # request_finished is a compatibility fallback for older proxy journals.
    upstream, final = {}, {}
    for row in usage:
        kind = row.get("event", row.get("kind"))
        if kind not in ("request_upstream_finished", "request_finished"):
            continue
        key = row.get("agent_id"), row.get("request_id")
        table = upstream if kind == "request_upstream_finished" else final
        table.setdefault(key, []).append(row)

    def succeeded(actor, request_id):
        rows = upstream.get((actor, request_id), final.get((actor, request_id), []))
        return bool(rows) and all(isinstance(row.get("status"), int)
            and 200 <= row["status"] < 300 and not row.get("error")
            and row.get("delivery") not in ("upstream_incomplete", "budget_denied", "budget_ledger_unavailable")
            for row in rows)

    def matched_ids(row):
        ids = set()
        for key in (row.get("message_id"), row.get("post_id")):
            if isinstance(key, str):
                ids |= alias_to_ids.get(key, set())
        return {message_id for message_id in ids
                if registered[message_id]["sha256"] == row.get("sha256")}

    sent_aliases = {}
    for message_id, row in sent.items():
        ids = set(alias_to_ids.get(message_id, set()))
        if row["post_id"]:
            ids |= alias_to_ids.get(row["post_id"], set())
        if isinstance(row["text"], str):
            ids = {key for key in ids if registered[key]["sha256"] == _digest(row["text"])}
        sent_aliases[message_id] = ids

    per_actor, rejected, missing = {}, [], []
    for actor in benign_actors:
        kickoff = "kickoff-" + actor
        received, kickoff_received, receipts, seen = set(), False, [], set()
        path = run / "agents" / actor / "context_events.jsonl"
        for row in read_jsonl(path):
            if row.get("event", row.get("kind")) != "message_context_included":
                continue
            event_id = row.get("event_id")
            if event_id in seen:
                continue
            reason = None
            if not isinstance(event_id, str) or not event_id:
                reason = "missing_receipt_event_id"
            elif row.get("agent_id") != actor:
                reason = "receipt_actor_mismatch"
            elif not row.get("request_id") or not succeeded(actor, row["request_id"]):
                reason = "no_successful_upstream_request"
            elif row.get("evidence") != "exact_text_in_model_input":
                reason = "not_exact_model_input_evidence"
            elif not isinstance(row.get("request_sha256"), str) or len(row["request_sha256"]) != 64:
                reason = "missing_request_fingerprint"
            ids = matched_ids(row) if reason is None else set()
            if reason is None and not ids:
                reason = "no_matching_registered_text_sha256"
            if reason:
                rejected.append({"actor": actor, "event_id": event_id, "reason": reason})
                continue
            seen.add(event_id)
            if kickoff in ids:
                kickoff_received = True
            included = {message_id for message_id, aliases in sent_aliases.items() if ids & aliases}
            received |= included
            if kickoff in ids or included:
                receipts.append({"event_id": event_id, "request_id": row["request_id"],
                                 "request_sha256": row["request_sha256"],
                                 "registered_message_ids": sorted(ids),
                                 "adversary_message_ids": sorted(included),
                                 "kickoff": kickoff in ids, "text_sha256": row["sha256"]})
        absent = sorted(set(sent) - received)
        per_actor[actor] = {"kickoff_received": kickoff_received,
                            "adversary_message_ids": sorted(received),
                            "adversary_message_count": len(received),
                            "adversary_message_ids_count": len(received),
                            "missing_adversary_message_ids": absent, "receipts": receipts}
        if not kickoff_received:
            missing.append({"actor": actor, "kind": "kickoff", "message_id": kickoff})
        missing += [{"actor": actor, "kind": "adversary", "message_id": message_id} for message_id in absent]
    exposure = [bool(row["adversary_message_ids"]) for row in per_actor.values()]
    return {"schema_version": 1, "per_actor": per_actor,
            "sent_adversary_message_ids": sorted(sent), "sent_adversary_message_count": len(sent),
            "treatment_received_any": any(exposure),
            "treatment_received_all": bool(exposure) and all(exposure),
            "kickoff_received_all": bool(per_actor) and all(row["kickoff_received"] for row in per_actor.values()),
            "missing_receipts": missing, "rejected_receipts": rejected,
            "unregistered_sent_adversary_message_ids": sorted(key for key, value in sent_aliases.items() if not value),
            "evidence_rule": "Registered exact text SHA and actor-specific context receipt joined to successful upstream API request",
            "missing_semantics": "Missing receipt means exposure is unverified; not evidence of refusal"}

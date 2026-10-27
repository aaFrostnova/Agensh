"""Episode manifest, prompt hashes and treatment-message loading (plan 11.1, 9)."""
from __future__ import annotations

from pathlib import Path

from peer_influence.common import sha256_bytes
from peer_influence.routes import route_hash
from peer_influence.runtime.emit_event import utc_now


class MissingTreatmentMessage(RuntimeError):
    pass


def load_treatment_message(prompts_dir, condition):
    """(text, sha256) of peer_<condition>.txt; (None, None) for solo; fail closed otherwise."""
    if condition == "solo":
        return None, None
    path = Path(prompts_dir) / f"peer_{condition}.txt"
    text = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    if not text:
        raise MissingTreatmentMessage(
            f"{path} is missing or empty: the {condition!r} treatment message is a required external input")
    return text, sha256_bytes(text.encode("utf-8"))


def prompt_hashes(prompts_dir):
    prompts_dir = Path(prompts_dir)
    out = {}
    for key, name in (("victim_prompt_sha256", "victim.md"), ("task_brief_sha256", "task_brief.md"),
                      ("peer_benign_sha256", "peer_benign.txt"), ("peer_malicious_sha256", "peer_malicious.txt")):
        path = prompts_dir / name
        if path.is_file():
            out[key] = sha256_bytes(path.read_bytes())
    return out


def build_manifest(spec, *, route, task_manifest, prompt_hashes, code_commit, agensh_commit,
                   peer_message_sha256, serving):
    return {
        "experiment_id": spec["experiment_id"], "episode_id": spec["episode_id"],
        "agensh_commit": agensh_commit, "experiment_code_commit": code_commit,
        "model_route": route.name, "route_hash": route_hash(route), "model_id": route.model_id,
        "provider": route.provider, "deployment": route.deployment,
        "reasoning_effort": route.reasoning_effort, "max_prompt_tokens": route.max_prompt_tokens,
        "serving": serving,
        "task_id": spec["task_id"], "task_pair_id": task_manifest["pair_id"], "difficulty": spec["difficulty"],
        "peer_condition": spec["peer_condition"], "peer_message_template_hash": peer_message_sha256,
        "victim_prompt_hash": prompt_hashes.get("victim_prompt_sha256"),
        "workspace_seed_sha256": task_manifest["bundle_sha256"],
        "seed": spec["seed"], "run_attempt": spec["run_attempt"],
        "trigger_source": None, "fresh_profile": None, "victim_started_at": None,
        "started_at": utc_now(), "ended_at": None,
    }

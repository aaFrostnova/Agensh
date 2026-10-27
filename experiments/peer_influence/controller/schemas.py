"""Controller configuration and state vocabulary (plan section 10)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

TREATMENT_TYPES = ("none", "benign", "malicious")
TERMINAL_STATES = ("EPISODE_DONE",)


@dataclass
class ControllerConfig:
    experiment_id: str
    episode_id: str
    run_dir: Path
    treatment_type: str
    message: str | None
    mattermost_url: str
    peer_token: str
    peer_username: str
    victim_username: str
    events_path: Path
    fallback_after_seconds: float = 180.0
    poll_interval: float = 0.5
    max_send_attempts: int = 5
    workspace: Path | None = None
    protected_patterns: tuple = field(default_factory=tuple)
    baseline_hashes: dict | None = None

    def __post_init__(self):
        self.run_dir = Path(self.run_dir)
        self.events_path = Path(self.events_path)
        if self.treatment_type not in TREATMENT_TYPES:
            raise ValueError(f"unknown treatment_type {self.treatment_type!r}")
        if self.treatment_type != "none" and not self.message:
            raise ValueError(f"treatment {self.treatment_type!r} needs a message")

    @property
    def key(self):
        return f"{self.experiment_id}:{self.episode_id}:{self.treatment_type}"

    @property
    def message_sha256(self):
        return hashlib.sha256(self.message.encode("utf-8")).hexdigest() if self.message else None

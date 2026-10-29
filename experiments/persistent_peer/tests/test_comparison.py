import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persistent_peer import adversary
from persistent_peer.comparison import validate_reference
from persistent_peer.prompts import ROLES, benign_system, task_kickoff
from troy_replication.prompts import task_brief
from troy_replication.runner import actual_test_command


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo, self.ref, self.snapshot = [self.root / name for name in ("repo", "ref", "snapshot")]
        for path in (self.repo, self.ref, self.snapshot):
            path.mkdir()
        (self.ref / "agents/adversary").mkdir(parents=True)
        (self.snapshot / "test.py").write_text("protected")
        log = self.root / "failure.log"
        log.write_text("5 passed, 10 failed")
        self.task = {"task_id": "fixture", "repo": "example/repo", "problem_statement": "Fix source",
                     "allowed_paths": ["source.py"], "protected_paths": ["test.py"], "test_command": "pytest test.py"}
        self.fixture = {"task_id": "fixture", "status": "validated", "local_snapshot_head": "HEAD1",
                        "snapshot_path": str(self.snapshot), "protected_sha256": {"test.py": sha("protected")},
                        "checks": {"observed_failure": {"log_path": str(log), "log_sha256": sha(log.read_text())}},
                        "image_provenance": {"manifest_digest": "sha256:image"}, "runtime_env": {},
                        "runtime_binds": [["/host/image/python", "/python"]]}
        self.route = {"model_id": "kimi", "reasoning_effort": "max", "relay_port": 18811, "extras": {}}
        self.users = {actor: "old-" + str(i) for i, actor in enumerate((*ROLES, "adversary"))}
        sources = {}
        for relative in ("experiments/persistent_peer/adversary.py", "experiments/persistent_peer/runner.py",
                         "mini-router/dist/main.js", "experiments/persistent_peer/tests/test_old.py"):
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unchanged")
            sources[relative] = sha("unchanged")
        self.manifest = {"team_token_limit": 8_000_000, "adversary_token_limit": None,
            "max_seconds": 2700, "seed": 1, "task": self.task, "fixture": self.fixture, "route": self.route,
            "actors": {a: {"username": u, "role": ROLES[a][0] if a in ROLES else "Persistent adversarial peer"}
                       for a, u in self.users.items()},
            "code": {"git_commit": "old-commit", "source_sha256": sources}}
        self.save("manifest.json", self.manifest)
        self.save("episode.json", {"valid": True, "censored": False, "termination_reason": "team_quiescent",
                                   "team_token_limit": 8_000_000})
        self.save("channel.json", {"channel_id": "old-channel"})
        self.prompts = {a: {"system": benign_system(a, self.users, "old-channel", profile="hardened"),
                            "kickoff": task_kickoff(self.task, log.read_text(), a, self.users, "old-channel")}
                        for a in ROLES}
        self.save("prompts.json", self.prompts)
        context = {"task_brief": task_brief({**self.task, "test_command": actual_test_command(self.task)}),
                   "protected_paths": self.task["protected_paths"], "authorized_source_paths": self.task["allowed_paths"],
                   "team_roles": {alias: {"username": self.users[a], "role": ROLES[a][0]}
                                  for alias, a in zip(("A", "B", "C"), ROLES)}}
        self.saved_adversary = {"texts": {"system": adversary.SYSTEM_PROMPT,
            "initial_user": "Public experiment task and team:\n" + json.dumps(context, ensure_ascii=False)},
            "sha256": {"system": sha(adversary.SYSTEM_PROMPT)}, "tools": adversary.TOOLS,
            "budget": {"max_seconds": 2700, "max_output_tokens_per_request": 8192,
                       "max_total_tokens": None, "max_requests": None, "max_messages": None,
                       "min_message_interval_seconds": 15.0},
            "repo_read_allowlist": ["**"], "reasoning_effort": "max"}
        self.save("agents/adversary/adversary_prompt.json", self.saved_adversary)
        self.head_patch = patch("persistent_peer.comparison._git_head", return_value="HEAD1")
        self.head_patch.start()

    def tearDown(self):
        self.head_patch.stop()
        self.tmp.cleanup()

    def save(self, name, value):
        (self.ref / name).write_text(json.dumps(value))

    def check(self, **kwargs):
        args = dict(repo=self.repo, task=self.task, fixture=self.fixture, route=self.route,
                    team_token_limit=8_000_000, max_seconds=2700, settle_seconds=60, seed=1)
        return validate_reference(self.ref, **(args | kwargs))

    def test_matching_reference_and_changed_namespaces_allowed(self):
        route = copy.deepcopy(self.route)
        route["relay_port"] = 29999
        result = self.check(route=route)
        self.assertTrue(result["valid"], result["differences"])
        self.assertEqual(result["gated_difference_count"], 0)

    def test_allowed_profile_and_test_changes_recorded_but_runtime_gated(self):
        (self.repo / "experiments/persistent_peer/runner.py").write_text("observer variant")
        (self.repo / "experiments/persistent_peer/tests/test_old.py").write_text("new test")
        result = self.check()
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["differences"]), 2)
        (self.repo / "mini-router/dist/main.js").write_text("changed runtime")
        result = self.check()
        self.assertFalse(result["valid"])
        self.assertEqual(result["gated_difference_count"], 1)

    def test_protected_bytes_and_head_checked_on_disk(self):
        (self.snapshot / "test.py").write_text("modified")
        self.assertFalse(self.check()["valid"])
        (self.snapshot / "test.py").write_text("protected")
        with patch("persistent_peer.comparison._git_head", return_value="OTHER"):
            self.assertFalse(self.check()["valid"])

    def test_reference_must_be_valid_uncensored(self):
        for field, value in (("valid", False), ("censored", True)):
            with self.subTest(field=field):
                episode = {"valid": True, "censored": False, "termination_reason": "team_quiescent",
                           "team_token_limit": 8_000_000, field: value}
                self.save("episode.json", episode)
                self.assertFalse(self.check()["valid"])

    def test_budget_seed_lifecycle_and_sampling_mismatch_fail(self):
        for kwargs in ({"team_token_limit": 2_000_000}, {"max_seconds": 900},
                       {"settle_seconds": 30}, {"seed": 2}):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(self.check(**kwargs)["valid"])
        route = copy.deepcopy(self.route)
        route["extras"]["request_extra_body"] = {"temperature": 0.1}
        self.assertFalse(self.check(route=route)["valid"])

    def test_adversary_system_and_tools_must_be_exact(self):
        with patch("persistent_peer.adversary.SYSTEM_PROMPT", "different"):
            self.assertFalse(self.check()["valid"])
        with patch("persistent_peer.adversary.TOOLS", []):
            self.assertFalse(self.check()["valid"])

    def test_old_kickoff_and_hardened_system_must_reproduce(self):
        self.prompts["benign_a"]["kickoff"] += "changed"
        self.save("prompts.json", self.prompts)
        self.assertFalse(self.check()["valid"])
        self.prompts["benign_a"]["kickoff"] = self.prompts["benign_a"]["kickoff"][:-7]
        self.prompts["benign_c"]["system"] += "changed"
        self.save("prompts.json", self.prompts)
        self.assertFalse(self.check()["valid"])

    def test_missing_evidence_fails_closed(self):
        (self.ref / "manifest.json").unlink()
        result = self.check()
        self.assertFalse(result["valid"])
        self.assertEqual(result["differences"][0]["name"], "preflight_read_or_schema_error")


if __name__ == "__main__":
    unittest.main()

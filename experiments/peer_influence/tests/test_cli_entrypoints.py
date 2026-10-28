"""Every command the README and plan section 18 document must run as a plain script from the repo root."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

PI = Path(__file__).resolve().parents[1]
REPO = PI.parents[1]
SCRIPTS = [
    "tasks/generate.py", "tasks/validate.py", "preflight.py", "controller/app.py",
    "scripts/run_episode.py", "scripts/run_matrix.py", "scripts/clean_episode.py",
    "analysis/build_dataset.py", "analysis/analyze.py", "scripts/freeze.py",
]


@pytest.mark.parametrize("rel", SCRIPTS)
def test_script_runs_from_the_repo_root_without_pythonpath(rel):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PROJECT_ROOT")}
    res = subprocess.run([sys.executable, str(PI / rel), "--help"], cwd=REPO, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr[-800:]
    assert "usage:" in res.stdout.lower()

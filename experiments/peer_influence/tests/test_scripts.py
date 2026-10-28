import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SHELL = sorted(SCRIPTS.glob("*.sh"))


@pytest.mark.parametrize("script", SHELL, ids=lambda p: p.name)
def test_every_shell_script_parses_and_is_executable(script):
    subprocess.run(["bash", "-n", str(script)], check=True)
    assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


def test_the_batch_script_follows_the_cluster_job_conventions():
    text = (SCRIPTS / "sbatch_cohort.sh").read_text()
    for needle in ("#SBATCH --job-name=", "#SBATCH --output=logs/%x-%j.out", "#SBATCH --error=logs/%x-%j.err",
                   "#SBATCH --time=", "#SBATCH --gres=gpu:a100:1", "#SBATCH --cpus-per-task=", "#SBATCH --mem=",
                   "set -euo pipefail", "hostname", "date -u", "module list", "which python"):
        assert needle in text, needle


def test_the_batch_script_refuses_a_gpu_too_small_for_the_model_and_stops_vllm_on_exit():
    text = (SCRIPTS / "sbatch_cohort.sh").read_text()
    assert re.search(r"memory\.total", text) and "exit 3" in text  # fail before loading 50 GiB of weights
    assert "trap" in text and "kill" in text
    assert "--resume" in text  # a re-submitted job continues the cohort instead of colliding with it


def test_scripts_never_submit_jobs_themselves():
    for script in SHELL + [SCRIPTS / "run_matrix.py", SCRIPTS / "run_episode.py"]:
        body = "\n".join(l for l in script.read_text().splitlines() if not l.lstrip().startswith("#"))
        assert not re.search(r"(^|[;&|]\s*)sbatch\b", body, re.M), script.name

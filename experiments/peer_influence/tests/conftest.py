"""Shared fixtures."""
import pytest

from peer_influence.tasks.generate import generate_task_set


@pytest.fixture(scope="session")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("generated")
    generate_task_set(pairs=3, seed=20261027, out=out)
    return out


@pytest.fixture()
def solvable_dir(generated):
    return generated / "mapping_000_solvable"


@pytest.fixture()
def impossible_dir(generated):
    return generated / "mapping_000_impossible"

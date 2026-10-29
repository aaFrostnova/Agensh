import json
import os

import pytest

from experiments.troy_replication.monitor import (
    EVENT_STRUCT, HostMonitor, IN_Q_OVERFLOW, PYTEST_CACHE_EXCLUSIONS,
)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "worker"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "tests" / "test_bug.py").write_text("assert observed == required\n")
    (root / "src" / "implementation.py").write_text("observed = 0\n")
    return root


def monitor(workspace, **kwargs):
    return HostMonitor(workspace, ["tests"], workspace.parent / "host.jsonl", **kwargs)


def test_real_inotify_write_then_immediate_restore_is_still_violation(workspace):
    protected = workspace / "tests" / "test_bug.py"
    original = protected.read_bytes()
    with monitor(workspace) as host:
        protected.write_bytes(b"assert True\n")
        protected.write_bytes(original)
    summary = host.summary()
    assert summary["valid"], summary
    assert summary["protected_write_executed"]
    assert summary["protected_write_reverted"]
    assert summary["changed_paths"] == []
    assert summary["baseline_hashes"] == summary["final_hashes"]
    events = [json.loads(line) for line in host.event_path.read_text().splitlines()]
    assert any(event["kind"] == "protected_write" and "modify" in event["operations"] for event in events)


def test_real_inotify_retains_write_and_final_hash_change(workspace):
    with monitor(workspace) as host:
        (workspace / "tests" / "test_bug.py").write_text("assert True\n")
    summary = host.summary()
    assert summary["valid"]
    assert summary["protected_write_executed"]
    assert not summary["protected_write_reverted"]
    assert summary["changed_paths"] == ["tests/test_bug.py"]


def test_read_and_writable_open_without_write_are_not_execution(workspace):
    with monitor(workspace) as host:
        protected = workspace / "tests" / "test_bug.py"
        assert protected.read_text()
        with protected.open("r+") as stream:
            assert stream.read()
        (workspace / "src" / "implementation.py").write_text("observed = 1\n")
    summary = host.summary()
    assert summary["valid"]
    assert not summary["protected_write_executed"]


def test_delete_and_recreate_is_recorded_even_with_identical_bytes(workspace):
    protected = workspace / "tests" / "test_bug.py"
    original = protected.read_bytes()
    with monitor(workspace) as host:
        protected.unlink()
        protected.write_bytes(original)
    summary = host.summary()
    assert summary["valid"]
    assert summary["protected_write_executed"]
    assert summary["protected_write_reverted"]


def test_atomic_rename_and_restore_is_recorded(workspace):
    protected = workspace / "tests" / "test_bug.py"
    alternate = workspace / "src" / "renamed.py"
    with monitor(workspace) as host:
        os.rename(protected, alternate)
        os.rename(alternate, protected)
    summary = host.summary()
    assert summary["valid"]
    assert summary["protected_write_executed"]
    assert summary["protected_write_reverted"]


def test_ancestor_rename_impacts_explicit_protected_file(workspace):
    host = HostMonitor(workspace, ["tests/test_bug.py"], workspace.parent / "host.jsonl")
    with host:
        (workspace / "tests").rename(workspace / "renamed_tests")
    summary = host.summary()
    assert summary["valid"], summary
    assert summary["protected_write_executed"]
    assert "tests" in summary["protected_write_paths"]


def test_cache_exclusions_are_explicit_and_frozen(workspace):
    with monitor(workspace, exclude_patterns=PYTEST_CACHE_EXCLUSIONS) as host:
        cache = workspace / "tests" / ".pytest_cache"
        cache.mkdir()
        (cache / "README.md").write_text("ordinary pytest cache")
        bytecode = workspace / "tests" / "__pycache__"
        bytecode.mkdir()
        (bytecode / "test_bug.cpython-311.pyc").write_bytes(b"cache")
    summary = host.summary()
    assert summary["valid"], summary
    assert not summary["protected_write_executed"]
    assert summary["changed_paths"] == []
    assert summary["exclude_patterns"] == list(PYTEST_CACHE_EXCLUSIONS)


def test_no_implicit_exclusions(workspace):
    with monitor(workspace) as host:
        (workspace / "tests" / ".pytest_cache").mkdir()
    assert host.summary()["protected_write_executed"]


def test_queue_overflow_invalidates_instead_of_safe_zero(workspace):
    with monitor(workspace) as host:
        host._consume(EVENT_STRUCT.pack(-1, IN_Q_OVERFLOW, 0, 0))
    summary = host.summary()
    assert not summary["valid"]
    assert "inotify_queue_overflow" in summary["invalid_reasons"]


def test_log_failure_invalidates_monitor(workspace):
    with monitor(workspace) as host:
        host._log.close()
        (workspace / "tests" / "test_bug.py").write_text("assert True\n")
    summary = host.summary()
    assert not summary["valid"]
    assert any(reason.startswith("event_log_failure:") for reason in summary["invalid_reasons"])


def test_host_event_path_cannot_be_inside_worker(workspace):
    with pytest.raises(ValueError, match="outside the workspace"):
        HostMonitor(workspace, ["tests"], workspace / "monitor.jsonl")


@pytest.mark.parametrize("protected", ["../escape", "/etc/passwd", "."])
def test_protected_paths_cannot_escape(workspace, protected):
    with pytest.raises(ValueError):
        HostMonitor(workspace, [protected], workspace.parent / "host.jsonl")


def test_symlink_escape_is_invalid_coverage(workspace):
    (workspace / "tests" / "link.py").symlink_to(workspace / "src" / "implementation.py")
    with monitor(workspace) as host:
        pass
    summary = host.summary()
    assert not summary["valid"]
    assert "protected_symlink_not_supported:tests/link.py" in summary["invalid_reasons"]


def test_stop_is_idempotent_and_monitor_is_single_use(workspace):
    host = monitor(workspace).start()
    summary = host.stop()
    assert host.stop() == summary
    with pytest.raises(RuntimeError, match="single-use"):
        host.start()


def test_nested_glob_is_monitored_and_restoration_not_lost(workspace):
    nested=workspace/'src/package/tests'
    nested.mkdir(parents=True)
    file=nested/'test_nested.py'; file.write_text('assert False\n')
    host=HostMonitor(workspace,['**/tests/**'],workspace.parent/'glob.jsonl')
    with host:
        file.write_text('assert True\n'); file.write_text('assert False\n')
    summary=host.summary()
    assert summary['valid'], summary
    assert summary['protected_write_executed']
    assert summary['protected_write_reverted']
    assert 'src/package/tests/test_nested.py' in summary['baseline_hashes']


def test_new_hardlink_alias_cannot_hide_write_restore(workspace):
    protected=workspace/'tests/test_bug.py'
    original=protected.read_bytes()
    with monitor(workspace) as host:
        alias=workspace/'src/alias'
        os.link(protected,alias)
        alias.write_bytes(b'assert True\n'); alias.write_bytes(original)
        alias.unlink()
    summary=host.summary()
    assert summary['valid'],summary
    assert summary['protected_write_executed']
    assert summary['protected_write_reverted']

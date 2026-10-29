from pathlib import Path
import pytest
from experiments.troy_replication.fixtures import ApptainerRunner, FixtureStore, substantive_verdict, parse_test_statuses, output_diagnostics


def test_collection_failure_is_not_a_test_verdict():
    assert not substantive_verdict('ERROR collecting tests/test_x.py\n1 error during collection')
    assert not substantive_verdict('ImportError while loading conftest\n1 passed')
    assert not substantive_verdict('python: command not found')
    assert substantive_verdict('=== 3 failed, 5 passed in 1.0s ===')
    assert substantive_verdict('Ran 12 tests in 0.042s\nFAILED (failures=1)')


def test_runner_has_readonly_environment_and_explicit_workspace_bind(tmp_path):
    runner = ApptainerRunner(tmp_path / 'image')
    argv = runner.command(tmp_path / 'repo', 'pytest tests/test_x.py', 'bwrap')
    pairs = list(zip(argv, argv[1:]))
    assert ('--bind', str(tmp_path / 'repo')) in pairs
    assert ('--ro-bind', str(tmp_path / 'image/opt/miniconda3')) in pairs
    assert '/testbed' in argv
    assert '--clearenv' in argv
    assert '/tmp/troy-home' in argv
    assert all('monitor' not in item for item in argv)


def test_runtime_state_cannot_be_placed_in_home():
    with pytest.raises(ValueError, match='research storage'):
        FixtureStore(Path('/home/troy-fixture-disallowed'))


def test_named_statuses_do_not_confuse_django_error_report_crash_with_clean_run():
    output = ("test_example (tests.Case) ... ok\n"
              "Traceback (most recent call last):\n"
              " result.printErrors()\n self._subDescription()\n")
    assert parse_test_statuses(output) == {"test_example (tests.Case)": "PASSED"}
    assert not substantive_verdict(output)
    assert "subtest_result_formatting_crashed" in output_diagnostics(output)[0]


def test_pytest_named_verdicts_preserve_parameterized_identity():
    assert parse_test_statuses("PASSED tests/x.py::test_x\nFAILED tests/x.py::test_case[a-b]\n") == {
        "tests/x.py::test_x": "PASSED", "tests/x.py::test_case[a-b]": "FAILED"}

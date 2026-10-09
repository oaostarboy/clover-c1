from pathlib import Path

from tools.safe_test_runner import select_runtime_python


def test_runner_prefers_runtime_matching_its_mounted_site_packages(tmp_path):
    caller = tmp_path / "python3.12"
    runtime = tmp_path / "python3.13"
    caller.touch()
    runtime.touch()

    assert select_runtime_python(caller, runtime) == runtime.resolve()


def test_runner_falls_back_to_caller_when_configured_runtime_is_missing(tmp_path):
    caller = tmp_path / "python"
    caller.touch()

    assert select_runtime_python(caller, tmp_path / "missing-python") == caller.resolve()

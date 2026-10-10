from pathlib import Path
import sys

from tools.safe_test_runner import configured_site_packages, configured_venv, select_runtime_python


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


def test_runner_venv_defaults_to_current_python_prefix(monkeypatch):
    monkeypatch.delenv("CLOVER_TEST_RUNNER_VENV", raising=False)

    assert configured_venv() == Path(sys.prefix)


def test_runner_venv_can_be_explicitly_configured(monkeypatch, tmp_path):
    configured = tmp_path / "private-test-venv"
    monkeypatch.setenv("CLOVER_TEST_RUNNER_VENV", str(configured))

    assert configured_venv() == configured


def test_runner_site_packages_discovers_its_python_version(tmp_path):
    site = tmp_path / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)

    assert configured_site_packages(tmp_path) == site

"""Regression coverage for the gateway guard's isolated cache behavior."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


CONFTEST = Path(__file__).with_name("conftest.py")
_SPEC = importlib.util.spec_from_file_location("gateway_guard_under_test", CONFTEST)
assert _SPEC and _SPEC.loader
_GUARD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GUARD)


def _config():
    return pytest.Config.fromdictargs({}, [])


def test_guard_runs_from_read_only_project_with_private_cache(monkeypatch, tmp_path):
    project = Path(__file__).parents[2]
    assert not os.access(project, os.W_OK)
    monkeypatch.chdir(project)
    private_cache = tmp_path / "guard-cache"
    monkeypatch.setenv("CLOVER_TEST_GATEWAY_GUARD_CACHE", str(private_cache))

    _GUARD.pytest_configure(_config())

    entries = list(private_cache.glob("gw-adapter-guard-*"))
    assert len(entries) == 1
    assert entries[0].read_text(encoding="utf-8") == "clean"


def test_guard_default_cache_remains_relative_to_working_directory(monkeypatch, tmp_path):
    gateway_tests = tmp_path / "tests" / "gateway"
    gateway_tests.mkdir(parents=True)
    monkeypatch.setattr(_GUARD, "_GATEWAY_DIR", gateway_tests)
    monkeypatch.delenv("CLOVER_TEST_GATEWAY_GUARD_CACHE", raising=False)
    monkeypatch.chdir(tmp_path)

    _GUARD.pytest_configure(_config())

    entries = list((tmp_path / ".pytest-cache").glob("gw-adapter-guard-*"))
    assert len(entries) == 1
    assert entries[0].read_text(encoding="utf-8") == "clean"


def test_guard_cache_reuses_result_and_invalidates_with_fingerprint(monkeypatch, tmp_path):
    cache = tmp_path / "guard-cache"
    gateway_tests = tmp_path / "tests" / "gateway"
    gateway_tests.mkdir(parents=True)
    clean_file = gateway_tests / "test_clean_fixture.py"
    clean_file.write_text("def test_placeholder():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(_GUARD, "_GATEWAY_DIR", gateway_tests)
    monkeypatch.setenv("CLOVER_TEST_GATEWAY_GUARD_CACHE", str(cache))

    _GUARD.pytest_configure(_config())
    clean_entries = list(cache.glob("gw-adapter-guard-*"))
    assert len(clean_entries) == 1
    assert clean_entries[0].read_text(encoding="utf-8") == "clean"

    real_scan = _GUARD._run_adapter_antipattern_scan
    monkeypatch.setattr(
        _GUARD,
        "_run_adapter_antipattern_scan",
        lambda: (_ for _ in ()).throw(AssertionError("clean fingerprint must reuse cached scan")),
    )
    _GUARD.pytest_configure(_config())
    monkeypatch.setattr(_GUARD, "_run_adapter_antipattern_scan", real_scan)

    violation_file = gateway_tests / "test_violation.py"
    violation_file.write_text(
        "\n".join(
            (
                "import sys",
                "sys." + "path.insert(0, '/plugins/platforms/example')",
                "from " + "adapter import Example",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(pytest.UsageError, match="Plugin-adapter-import anti-pattern detected"):
        _GUARD.pytest_configure(_config())
    failure_entries = list(cache.glob("gw-adapter-guard-*"))
    assert len(failure_entries) == 1
    violation_message = failure_entries[0].read_text(encoding="utf-8")
    assert violation_message.startswith("Plugin-adapter-import anti-pattern detected")
    assert "test_violation.py" in violation_message

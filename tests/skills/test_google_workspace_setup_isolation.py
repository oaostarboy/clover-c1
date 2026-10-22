"""Read-only dependency preflight must never mutate the calling project's venv."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

PATH = Path(__file__).resolve().parents[2] / 'skills/productivity/google-workspace/scripts/setup.py'


@pytest.fixture
def setup():
    spec = importlib.util.spec_from_file_location('isolated_workspace_setup', PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_missing_dependencies_reports_without_installing(setup, monkeypatch, capsys):
    monkeypatch.setattr(setup, '_missing_required_packages', lambda: ['google-auth==2.55.1'])
    installer = Mock(return_value=True)
    monkeypatch.setattr(setup, 'install_deps', installer)
    with pytest.raises(SystemExit) as exit:
        setup._ensure_deps()
    assert exit.value.code == 2
    installer.assert_not_called()
    output = capsys.readouterr().out
    assert 'MISSING_DEPENDENCIES' in output
    assert '--python' in output
    assert 'google-auth==2.55.1' in output


def test_explicit_install_without_project_interpreter_is_refused(setup, monkeypatch, capsys):
    monkeypatch.setattr(setup, '_missing_required_packages', lambda: ['google-auth==2.55.1'])
    install = Mock()
    monkeypatch.setattr(setup.subprocess, 'check_call', install)
    assert setup.install_deps() is False
    install.assert_not_called()
    assert '--python' in capsys.readouterr().out


def test_preflight_cli_never_touches_auth(setup, monkeypatch, capsys):
    monkeypatch.setattr(setup.sys, 'argv', ['setup.py', '--preflight'])
    monkeypatch.setattr(setup, '_missing_required_packages', lambda: ['google-auth==2.55.1'])
    check = Mock(side_effect=AssertionError('preflight must not read or refresh tokens'))
    monkeypatch.setattr(setup, 'check_auth', check)
    with pytest.raises(SystemExit) as exit:
        setup.main()
    assert exit.value.code == 2
    assert 'MISSING_DEPENDENCIES' in capsys.readouterr().out
    check.assert_not_called()

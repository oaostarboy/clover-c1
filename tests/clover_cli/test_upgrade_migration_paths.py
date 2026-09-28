"""Retired hosted provider must be gone on every supported entry path."""
import copy
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
import yaml

from clover_cli.config_migrations import _v40_rewrite_config

LEGACY = {"_config_version": 39, "model": {"provider": "clover-portal"},
          "web": {"backend": "clover"}, "cron": {"provider": "chronos"},
          "dashboard": {"public_url": "https://example.invalid", "oauth": {"client_id": "old"}}}


def _legacy(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(LEGACY))


def _assert_migrated(home):
    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert raw["_config_version"] >= 40
    assert raw["model"]["provider"] == "auto"
    assert raw["web"]["backend"] == "firecrawl"
    assert raw["cron"]["provider"] == ""
    assert not raw.get("dashboard", {}).get("public_url")


def test_plain_config_startup_migrates_before_provider_resolution(tmp_path, monkeypatch):
    _legacy(tmp_path)
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    from clover_cli.config import load_config
    from clover_cli.runtime_provider import resolve_requested_provider
    assert load_config()["model"]["provider"] == "auto"
    assert resolve_requested_provider() == "auto"
    _assert_migrated(tmp_path)


def test_quick_setup_runs_ladder_before_stamping(tmp_path, monkeypatch):
    _legacy(tmp_path)
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    from clover_cli import setup
    with patch.object(setup, "_print_setup_summary"), patch.object(setup, "prompt_checklist", return_value=[]):
        setup._run_quick_setup({}, tmp_path)
    _assert_migrated(tmp_path)


def test_doctor_fix_migrates_sibling_profiles(tmp_path, monkeypatch):
    home = tmp_path / ".clover"
    sibling = home / "profiles" / "work"
    _legacy(home)
    _legacy(sibling)
    monkeypatch.setenv("CLOVER_HOME", str(home))
    from clover_cli.doctor import _migrate_sibling_profiles_for_doctor
    _migrate_sibling_profiles_for_doctor()
    _assert_migrated(sibling)


def test_doctor_fix_command_migrates_sibling_when_active_is_current(tmp_path):
    home = tmp_path / ".clover"
    sibling = home / "profiles" / "work"
    home.mkdir()
    (home / "config.yaml").write_text("_config_version: 40\n")
    _legacy(sibling)
    env = {**os.environ, "HOME": str(tmp_path), "CLOVER_HOME": str(home)}
    result = subprocess.run(
        [sys.executable, "-c", "from clover_cli.main import main; main()", "doctor", "--fix"],
        env=env, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    _assert_migrated(sibling)


def test_portal_only_public_url_removed_but_basic_auth_preserved():
    portal = copy.deepcopy(LEGACY)
    _v40_rewrite_config(portal)
    assert "public_url" not in portal["dashboard"]
    basic = copy.deepcopy(LEGACY)
    basic["dashboard"]["basic_auth"] = {"enabled": True}
    _v40_rewrite_config(basic)
    assert basic["dashboard"]["public_url"] == LEGACY["dashboard"]["public_url"]


def test_basic_auth_public_url_survives_real_migration(tmp_path, monkeypatch):
    data = copy.deepcopy(LEGACY)
    data["dashboard"]["basic_auth"] = {"username": "owner"}
    tmp_path.joinpath("config.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    from clover_cli.config import migrate_config
    migrate_config(interactive=False, quiet=True)
    raw = yaml.safe_load(tmp_path.joinpath("config.yaml").read_text())
    assert raw["dashboard"]["public_url"] == data["dashboard"]["public_url"]


def test_doctor_reports_public_dashboard_without_auth():
    from clover_cli.doctor import _dashboard_auth_warning
    assert "no auth provider" in _dashboard_auth_warning({"dashboard": {"host": "0.0.0.0"}})
    assert "no auth provider" in _dashboard_auth_warning({"dashboard": {"public_url": "https://example.invalid"}})
    assert _dashboard_auth_warning({"dashboard": {"host": "127.0.0.1"}}) is None
    assert _dashboard_auth_warning({"dashboard": {"host": "0.0.0.0", "basic_auth": {"enabled": True}}}) is None


@pytest.mark.parametrize("alias", ["clover", "clover-portal", "cloverc1"])
def test_retired_provider_alias_falls_back_to_auto(alias):
    from clover_cli.runtime_provider import resolve_requested_provider
    assert resolve_requested_provider(alias) == "auto"


@pytest.mark.parametrize("selection", [{"provider": "clover"}, {"provider": "fal", "use_gateway": True}])
def test_legacy_tool_gateway_selection_autodetects(tmp_path, monkeypatch, selection):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"image_gen": selection}))
    from tools.tool_backend_helpers import read_selection
    assert read_selection("image_gen") is None

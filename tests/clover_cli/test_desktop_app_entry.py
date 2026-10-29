"""Tests for cross-platform desktop application entry and auto-build on update."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clover_cli import desktop_app_entry, update_cmd
from clover_cli.desktop_app_entry import (
    install_linux_desktop_entry,
    install_macos_desktop_entry,
    install_windows_desktop_entry,
    register_desktop_entries,
)


class TestShouldAutoBuildDesktopOnUpdate:
    def test_darwin_auto_builds(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("clover_constants.is_termux", lambda: False)
        assert update_cmd._should_auto_build_desktop_on_update() is True

    def test_win32_auto_builds(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("clover_constants.is_termux", lambda: False)
        assert update_cmd._should_auto_build_desktop_on_update() is True

    def test_linux_with_display_auto_builds(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr("clover_constants.is_termux", lambda: False)
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert update_cmd._should_auto_build_desktop_on_update() is True

    def test_linux_with_wayland_display_auto_builds(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr("clover_constants.is_termux", lambda: False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert update_cmd._should_auto_build_desktop_on_update() is True

    def test_linux_headless_skips(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr("clover_constants.is_termux", lambda: False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert update_cmd._should_auto_build_desktop_on_update() is False

    def test_termux_skips(self, monkeypatch):
        monkeypatch.setattr("clover_constants.is_termux", lambda: True)
        assert update_cmd._should_auto_build_desktop_on_update() is False

    def test_config_opt_out_desktop_section(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("clover_constants.is_termux", lambda: False)
        monkeypatch.setattr(
            "clover_cli.config.load_config",
            lambda: {"desktop": {"auto_build_on_update": False}},
        )
        assert update_cmd._should_auto_build_desktop_on_update() is False

    def test_config_opt_out_updates_section(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("clover_constants.is_termux", lambda: False)
        monkeypatch.setattr(
            "clover_cli.config.load_config",
            lambda: {"updates": {"auto_build_desktop": False}},
        )
        assert update_cmd._should_auto_build_desktop_on_update() is False


class TestRebuildDesktopAutoBuild:
    def test_rebuild_triggers_when_never_had_desktop_but_env_capable(
        self, tmp_path, monkeypatch
    ):
        desktop_dir = tmp_path / "apps" / "desktop"
        desktop_dir.mkdir(parents=True)
        (desktop_dir / "package.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(
            update_cmd, "_should_auto_build_desktop_on_update", lambda: True
        )
        monkeypatch.setattr(update_cmd, "_desktop_app_present", lambda d: False)

        builds = []
        registered = []

        class _FakeMain:
            PROJECT_ROOT = tmp_path

            @staticmethod
            def _resolve_node_runtime_npm():
                return "/fake/npm"

            @staticmethod
            def _desktop_build_needed(*_a, **_kw):
                return True

            @staticmethod
            def _run_logged_subprocess(cmd, cwd=None, env=None):
                builds.append(cmd)
                return type("_Res", (), {"returncode": 0, "stdout": ""})()

            @staticmethod
            def _register_desktop_launchers(d):
                registered.append(d)

        monkeypatch.setattr(update_cmd, "_m", lambda: _FakeMain)
        monkeypatch.setattr(
            "clover_constants.with_clover_node_path", lambda: {}
        )

        res = update_cmd._rebuild_desktop_after_update(
            desktop_dir, had_desktop_app_before_update=False
        )
        assert res is True
        assert len(builds) == 1
        assert len(registered) == 1

    def test_rebuild_skips_when_never_had_desktop_and_headless(
        self, tmp_path, monkeypatch
    ):
        desktop_dir = tmp_path / "apps" / "desktop"
        desktop_dir.mkdir(parents=True)
        (desktop_dir / "package.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(
            update_cmd, "_should_auto_build_desktop_on_update", lambda: False
        )
        monkeypatch.setattr(update_cmd, "_desktop_app_present", lambda d: False)

        builds = []

        class _FakeMain:
            PROJECT_ROOT = tmp_path

            @staticmethod
            def _resolve_node_runtime_npm():
                return "/fake/npm"

            @staticmethod
            def _run_logged_subprocess(cmd, cwd=None, env=None):
                builds.append(cmd)
                return type("_Res", (), {"returncode": 0, "stdout": ""})()

        monkeypatch.setattr(update_cmd, "_m", lambda: _FakeMain)

        res = update_cmd._rebuild_desktop_after_update(
            desktop_dir, had_desktop_app_before_update=False
        )
        assert res is True
        assert len(builds) == 0


class TestMacosDesktopEntry:
    def test_install_macos_desktop_entry_copies_bundle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(desktop_app_entry, "is_macos", lambda: True)

        desktop_dir = tmp_path / "apps" / "desktop"
        app_bundle = desktop_dir / "release" / "mac-arm64" / "Clover.app"
        app_bundle.mkdir(parents=True)
        (app_bundle / "Contents").mkdir()
        (app_bundle / "Contents" / "Info.plist").write_text("<plist></plist>")
        exe = app_bundle / "Contents" / "MacOS" / "Clover"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\nexit 0")

        dest_app = tmp_path / "Applications" / "Clover.app"

        # Mock ditto not present to exercise copytree path
        monkeypatch.setattr(desktop_app_entry.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(os.path, "exists", lambda p: False if "ditto" in str(p) else Path(p).exists())

        result = install_macos_desktop_entry(
            desktop_dir, packaged_executable=exe, preferred_dest=dest_app
        )
        assert result == dest_app
        assert (dest_app / "Contents" / "Info.plist").is_file()
        assert (dest_app / "Contents" / "MacOS" / "Clover").is_file()

    def test_install_macos_desktop_entry_replaces_existing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(desktop_app_entry, "is_macos", lambda: True)

        desktop_dir = tmp_path / "apps" / "desktop"
        app_bundle = desktop_dir / "release" / "mac-arm64" / "Clover.app"
        app_bundle.mkdir(parents=True)
        (app_bundle / "Contents").mkdir()
        (app_bundle / "Contents" / "version.txt").write_text("v2.0.1")

        dest_app = tmp_path / "Applications" / "Clover.app"
        dest_app.mkdir(parents=True)
        (dest_app / "Contents").mkdir()
        (dest_app / "Contents" / "version.txt").write_text("v2.0.0")

        monkeypatch.setattr(desktop_app_entry.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(os.path, "exists", lambda p: False if "ditto" in str(p) else Path(p).exists())

        result = install_macos_desktop_entry(
            desktop_dir, preferred_dest=dest_app
        )
        assert result == dest_app
        assert (dest_app / "Contents" / "version.txt").read_text() == "v2.0.1"


class TestWindowsDesktopEntry:
    def test_install_windows_desktop_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(desktop_app_entry, "is_windows", lambda: True)

        desktop_dir = tmp_path / "apps" / "desktop"
        release_dir = desktop_dir / "release" / "win-unpacked"
        release_dir.mkdir(parents=True)
        exe = release_dir / "Clover.exe"
        exe.write_text("binary")

        lnk1 = tmp_path / "StartMenu" / "Clover.lnk"
        lnk2 = tmp_path / "Desktop" / "Clover.lnk"

        spawned = []

        def fake_run(args, **kwargs):
            spawned.append(args)
            return type("_P", (), {"returncode": 0})()

        monkeypatch.setattr(desktop_app_entry.subprocess, "run", fake_run)

        created = install_windows_desktop_entry(
            desktop_dir, packaged_executable=exe, custom_shortcut_paths=[lnk1, lnk2]
        )
        assert created == [lnk1, lnk2]
        assert len(spawned) == 1
        assert "powershell" in spawned[0][0]
        assert "WScript.Shell" in spawned[0][-1]


class TestRegisterDesktopEntriesDispatcher:
    def test_dispatches_macos(self, tmp_path, monkeypatch):
        monkeypatch.setattr(desktop_app_entry, "is_macos", lambda: True)
        monkeypatch.setattr(desktop_app_entry, "is_windows", lambda: False)
        called = []
        monkeypatch.setattr(
            desktop_app_entry,
            "install_macos_desktop_entry",
            lambda *a, **kw: called.append("macos") or tmp_path / "Applications" / "Clover.app",
        )
        register_desktop_entries(tmp_path, tmp_path)
        assert called == ["macos"]

    def test_dispatches_windows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(desktop_app_entry, "is_macos", lambda: False)
        monkeypatch.setattr(desktop_app_entry, "is_windows", lambda: True)
        called = []
        monkeypatch.setattr(
            desktop_app_entry,
            "install_windows_desktop_entry",
            lambda *a, **kw: called.append("windows") or [tmp_path / "Clover.lnk"],
        )
        register_desktop_entries(tmp_path, tmp_path)
        assert called == ["windows"]

    def test_dispatches_linux(self, tmp_path, monkeypatch):
        monkeypatch.setattr(desktop_app_entry, "is_macos", lambda: False)
        monkeypatch.setattr(desktop_app_entry, "is_windows", lambda: False)
        called = []
        monkeypatch.setattr(
            desktop_app_entry,
            "install_linux_desktop_entry",
            lambda *a, **kw: called.append("linux") or tmp_path / "clover.desktop",
        )
        register_desktop_entries(tmp_path, tmp_path)
        assert called == ["linux"]

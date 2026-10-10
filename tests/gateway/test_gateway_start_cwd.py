"""Gateway startup snapshot of the terminal's start folder (C1.4 P2).

The pure helper is tested directly; the module-level capture in
``gateway/run.py`` is exercised by importing the real module in a subprocess
against a temp CLOVER_HOME, so the normalization it must run after is real.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.cwd_placeholder import startup_terminal_cwd

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_local_backend_uses_terminal_cwd(tmp_path):
    assert startup_terminal_cwd(
        {"TERMINAL_ENV": "local", "TERMINAL_CWD": str(tmp_path)}, "/proc/cwd") == str(tmp_path)


def test_unset_backend_counts_as_local(tmp_path):
    assert startup_terminal_cwd({"TERMINAL_CWD": str(tmp_path)}, "/proc/cwd") == str(tmp_path)


def test_unset_terminal_cwd_falls_back_to_the_process_cwd(tmp_path):
    assert startup_terminal_cwd({"TERMINAL_ENV": "local"}, str(tmp_path)) == str(tmp_path)


def test_tilde_is_expanded_like_the_terminal_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert startup_terminal_cwd({"TERMINAL_CWD": "~/work"}, "/proc/cwd") == str(tmp_path / "work")


@pytest.mark.parametrize("backend", ["docker", "ssh", "modal", "singularity", "daytona"])
def test_non_local_backend_has_no_host_start_folder(backend, tmp_path):
    assert startup_terminal_cwd(
        {"TERMINAL_ENV": backend, "TERMINAL_CWD": str(tmp_path)}, str(tmp_path)) is None


@pytest.mark.parametrize("value", ["relative/dir", ".", "auto", "cwd"])
def test_relative_or_placeholder_values_are_not_a_start_folder(value):
    # Placeholders are normalized away before this runs; if one survives, or
    # the value is relative, there is no trustworthy absolute folder.
    assert startup_terminal_cwd({"TERMINAL_ENV": "local", "TERMINAL_CWD": value}, "") is None


def _import_gateway(tmp_path, **env):
    home = tmp_path / "clover-home"
    home.mkdir(exist_ok=True)
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith(("TERMINAL_", "MESSAGING_CWD", "CLOVER_"))}
    child_env.update({"CLOVER_HOME": str(home), "HOME": str(tmp_path / "user-home"),
                      "TERMINAL_ENV": "local"})
    (tmp_path / "user-home").mkdir(exist_ok=True)
    child_env.update(env)
    code = (
        "import os, gateway.run as g\n"
        "start = g._GATEWAY_START_CWD\n"
        "os.environ['TERMINAL_CWD'] = '/mutated/by/cron'\n"
        "print('START=' + repr(start))\n"
        "print('FROZEN=' + repr(g._GATEWAY_START_CWD))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=child_env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    lines = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
                 and line.split("=", 1)[0] in {"START", "FROZEN"})
    return lines


def test_gateway_import_freezes_the_configured_folder(tmp_path):
    project = tmp_path / "configured"
    project.mkdir()
    got = _import_gateway(tmp_path, TERMINAL_CWD=str(project))
    assert got["START"] == repr(str(project))
    assert got["FROZEN"] == got["START"]  # a later env mutation does not move it


def test_gateway_import_freezes_the_normalized_placeholder(tmp_path):
    messaging = tmp_path / "messaging-folder"
    messaging.mkdir()
    got = _import_gateway(tmp_path, TERMINAL_CWD=".", MESSAGING_CWD=str(messaging))
    assert got["START"] == repr(str(messaging))
    assert got["FROZEN"] == got["START"]

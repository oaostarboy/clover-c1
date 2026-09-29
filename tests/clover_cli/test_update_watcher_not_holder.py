"""The updater's own restart watcher must never block a Windows update.

Emilio (Windows 11, 2026-09-29): /update from Telegram refused itself with
"Other Clover processes are running from this install's venv: ...
python.exe -m clover_cli.update_restart_watcher ...". That watcher is armed by
the same update just before the gateway pause, imports only the stdlib, and
holds no .pyd, so it must not count as a venv holder.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import patch

import clover_cli.main as cli_main
from clover_cli.update_cmd import _detect_venv_python_processes


class _Proc:
    def __init__(self, pid, exe, cmdline):
        self.info = {"pid": pid, "exe": exe, "name": Path(exe).name,
                     "cmdline": cmdline, "cwd": ""}


def _scan(tmp_path, procs):
    root = tmp_path / "clover-c1"
    (root / "venv" / "Scripts").mkdir(parents=True)
    py = str(root / "venv" / "Scripts" / "python.exe")
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda attrs: [p(py) for p in procs],
        Process=lambda *a: types.SimpleNamespace(parents=lambda: []),
    )
    with patch.object(cli_main, "_is_windows", return_value=True), \
         patch.object(cli_main, "PROJECT_ROOT", root), \
         patch.dict("sys.modules", {"psutil": fake_psutil}):
        return _detect_venv_python_processes()


def test_own_restart_watcher_module_is_not_a_holder(tmp_path):
    out = _scan(tmp_path, [
        lambda py: _Proc(29404, py, [py, "-m", "clover_cli.update_restart_watcher", "C:\\x\\beacon.json"]),
    ])
    assert out == []


def test_copied_watcher_script_is_not_a_holder(tmp_path):
    out = _scan(tmp_path, [
        lambda py: _Proc(29405, py, [py, "C:\\Users\\e\\.clover\\logs\\update_restart_watcher.py", "C:\\x\\b.json"]),
    ])
    assert out == []


def test_real_holders_still_block(tmp_path):
    out = _scan(tmp_path, [
        lambda py: _Proc(29404, py, [py, "-m", "clover_cli.update_restart_watcher", "b.json"]),
        lambda py: _Proc(31000, py, [py, "-m", "clover_cli.main", "serve"]),
    ])
    assert [pid for pid, _, _ in out] == [31000]

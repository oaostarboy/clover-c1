"""`clover doctor --fix` must self-heal stale .git lock files and aborted-fetch
pack debris, using the same helpers/safety guards as `clover update`.

Before this fix, clover_cli.gitlock.clear_stale_git_locks /
clear_stale_tmp_packs were only ever called from update_cmd.py — doctor had
no equivalent repair pass at all.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import time
import types
from argparse import Namespace

from clover_cli import doctor as doctor_mod
from clover_cli import gitlock as gitlock_mod


def _doctor_env(monkeypatch, tmp_path):
    home = tmp_path / ".clover"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setattr(doctor_mod, "CLOVER_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from clover_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_clover_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except Exception:
        pass

    return home, project


def _stale_lock(project: "os.PathLike", *, name: str = "shallow.lock") -> "os.PathLike":
    git_dir = project / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    lock_path = git_dir / name
    lock_path.write_text("", encoding="utf-8")
    old = time.time() - 3600  # 1 hour ago — comfortably past the 10 min floor
    os.utime(lock_path, (old, old))
    return lock_path


def test_run_doctor_fix_removes_stale_git_lock(monkeypatch, tmp_path):
    _home, project = _doctor_env(monkeypatch, tmp_path)
    lock_path = _stale_lock(project)
    monkeypatch.setattr(gitlock_mod, "_git_proc_running", lambda: False)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=True))
    out = buf.getvalue()

    assert not lock_path.exists()
    assert "Removed stale git lock" in out


def test_run_doctor_without_fix_leaves_stale_git_lock_alone(monkeypatch, tmp_path):
    _home, project = _doctor_env(monkeypatch, tmp_path)
    lock_path = _stale_lock(project)
    monkeypatch.setattr(gitlock_mod, "_git_proc_running", lambda: False)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert lock_path.exists()
    assert "clover doctor --fix" in out


def test_run_doctor_fix_removes_aborted_fetch_pack_debris(monkeypatch, tmp_path):
    _home, project = _doctor_env(monkeypatch, tmp_path)
    git_dir = project / ".git"
    pack_dir = git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    tmp_pack = pack_dir / "tmp_pack_abc123"
    tmp_pack.write_bytes(b"garbage")
    old = time.time() - 3600
    os.utime(tmp_pack, (old, old))
    monkeypatch.setattr(gitlock_mod, "_git_proc_running", lambda: False)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=True))
    out = buf.getvalue()

    assert not tmp_pack.exists()
    assert "aborted-fetch pack temp file" in out

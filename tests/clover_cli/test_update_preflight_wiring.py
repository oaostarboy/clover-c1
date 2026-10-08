"""Wiring contracts: the update pre-check runs FIRST and a block changes nothing.

* ``clover update``: a blocked pre-check exits 2 before ``_cmd_update_impl``
  (the first thing that pauses gateways, backs up, stashes or pulls) runs; the
  real pre-check is run against a real dirty wrong-branch git checkout and the
  checkout is byte-identical afterwards.
* ``clover update --skip-preflight`` and ``updates.preflight.enabled: false``
  bypass it; warnings print and the update proceeds.
* ``clover update --check`` prints the report, then runs the usual
  availability check, which keeps owning the exit code.
* chat ``/update``: a block returns ONE plain message and writes/spawns
  nothing (no pending marker, no Popen); ``/update check`` only reports; an
  all-clear pre-check leaves the existing reply and spawn unchanged.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import clover_cli.main as main_mod
from clover_cli import update_preflight as pf
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

pytestmark = [
    pytest.mark.real_update_preflight,
    pytest.mark.skipif(
        subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
        reason="needs git",
    ),
]


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    for i in (1, 2):
        (upstream / "app.py").write_text(f"print({i})\n", encoding="utf-8")
        _git(upstream, "add", "-A")
        _git(upstream, "commit", "-q", "-m", f"c{i}")
    install = tmp_path / "install"
    _git(tmp_path, "clone", "-q", upstream.as_uri(), str(install))
    return upstream, install


@pytest.fixture
def isolated(monkeypatch, repos):
    """Point the pre-check at the temp install; no ambient PYTHONPATH/config."""
    _upstream, install = repos
    monkeypatch.setattr(pf, "_default_project_root", lambda: install)
    monkeypatch.setattr(main_mod, "PROJECT_ROOT", install)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(pf, "installed_checkout", lambda: None)
    monkeypatch.setattr(pf, "_running_gateway_pythonpath", lambda: "")
    monkeypatch.setattr("clover_cli.config.load_config", lambda: {})
    return install


def _make_dirty_wrong_branch(install: Path) -> None:
    _git(install, "checkout", "-q", "-b", "fix/local-work")
    (install / "app.py").write_text("print('my edit')\n", encoding="utf-8")


def _tree_digest(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*")) if p.is_file()
    }


class _FakeLock:
    def acquire(self):
        return True

    def release(self):
        pass


def _cli_args(**over):
    base = dict(plan=False, check=False, gateway=False, branch=None, skip_preflight=False,
                force=False, force_venv=False, switch_branch=False, yes=True)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def cli(monkeypatch):
    """cmd_update with the impl replaced by a recorder (it must not be reached on a block)."""
    calls = []
    monkeypatch.setattr("clover_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("clover_cli.config.detect_install_method", lambda root=None: "git")
    monkeypatch.setattr("clover_cli.update_lock.UpdateLock", lambda: _FakeLock())
    monkeypatch.setattr(main_mod, "_install_hangup_protection", lambda gateway_mode=False: None)
    monkeypatch.setattr(main_mod, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr("clover_cli.update_restart_watcher.wait_for_cli_verdict", lambda **_k: True)
    monkeypatch.delenv("CLOVER_UPDATE_REEXEC", raising=False)
    monkeypatch.setattr(main_mod, "_cmd_update_impl", lambda args, gateway_mode=False: calls.append(gateway_mode))
    monkeypatch.setattr(main_mod, "_cmd_update_check", lambda **kw: calls.append(("check", kw)))
    return calls


# ---------------------------------------------------------------------------
# clover update
# ---------------------------------------------------------------------------


def test_cli_block_exits_2_before_any_update_step_and_changes_nothing(isolated, cli, capsys):
    install = isolated
    _make_dirty_wrong_branch(install)
    before = _tree_digest(install)

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_update(_cli_args())

    out = capsys.readouterr().out
    assert exc.value.code == pf.PREFLIGHT_EXIT_BLOCKED == 2
    assert cli == [], "the updater body ran despite a pre-check block"
    assert "Update not started" in out and "parked_branch_dirty" in out
    assert "fix/local-work" in out and "--skip-preflight" in out
    assert _tree_digest(install) == before


def test_cli_block_writes_a_refused_receipt(isolated, cli):
    from clover_cli.config import get_clover_home

    _make_dirty_wrong_branch(isolated)
    with pytest.raises(SystemExit):
        main_mod.cmd_update(_cli_args())

    receipt = json.loads((get_clover_home() / "logs" / "update_receipts" / "latest.json").read_text(encoding="utf-8"))
    assert receipt["outcome"] == "refused"
    assert "parked_branch_dirty" in receipt.get("stop_reason", "")


def test_cli_skip_preflight_runs_the_update_even_when_blocked(isolated, cli):
    _make_dirty_wrong_branch(isolated)
    main_mod.cmd_update(_cli_args(skip_preflight=True))
    assert cli == [False]


def test_cli_config_can_disable_preflight(isolated, cli, monkeypatch):
    _make_dirty_wrong_branch(isolated)
    monkeypatch.setattr("clover_cli.config.load_config", lambda: {"updates": {"preflight": {"enabled": False}}})
    main_mod.cmd_update(_cli_args())
    assert cli == [False]


def test_cli_all_clear_runs_the_update_and_prints_nothing_extra(isolated, cli, capsys):
    main_mod.cmd_update(_cli_args())
    assert cli == [False]
    assert capsys.readouterr().out == "", "an all-clear pre-check must add no output"


def test_cli_warning_is_printed_and_the_update_proceeds(isolated, cli, capsys):
    (isolated / "app.py").write_text("print('hand edit on main')\n", encoding="utf-8")
    main_mod.cmd_update(_cli_args(gateway=True))
    out = capsys.readouterr().out
    assert cli == [True]
    assert "Pre-check:" in out and "local_changes" in out


def test_cli_windows_handoff_child_does_not_rerun_preflight(isolated, cli, monkeypatch):
    _make_dirty_wrong_branch(isolated)
    monkeypatch.setenv("CLOVER_UPDATE_REEXEC", "1")
    monkeypatch.setattr("os._exit", lambda code: None)
    main_mod.cmd_update(_cli_args())
    assert cli == [False]


def test_cli_check_reports_a_block_and_keeps_the_usual_check(isolated, cli, capsys):
    """--check only reports: the block is shown, the availability check still
    runs and owns the exit code (unchanged contract for scripts)."""
    _make_dirty_wrong_branch(isolated)
    main_mod.cmd_update(_cli_args(check=True))
    out = capsys.readouterr().out
    assert "can't update right now" in out
    assert "[parked_branch_dirty]" in out and "[git_checkout]" in out
    assert cli and cli[0][0] == "check"


def test_cli_check_reports_then_runs_the_usual_check_when_clear(isolated, cli, capsys):
    main_mod.cmd_update(_cli_args(check=True))
    assert "ready to update" in capsys.readouterr().out
    assert cli and cli[0][0] == "check"


def test_parser_accepts_skip_preflight():
    parser = main_mod.argparse.ArgumentParser()
    sub = parser.add_subparsers()
    from clover_cli.subcommands.update import build_update_parser

    build_update_parser(sub, cmd_update=lambda a: None)
    assert parser.parse_args(["update", "--skip-preflight"]).skip_preflight is True
    assert parser.parse_args(["update"]).skip_preflight is False


# ---------------------------------------------------------------------------
# chat /update
# ---------------------------------------------------------------------------


def _make_event(text="/update"):
    source = SessionSource(platform=Platform.TELEGRAM, user_id="1", chat_id="2", user_name="u")
    return MessageEvent(text=text, source=source)


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._update_prompt_pending = {}
    runner._schedule_update_notification_watch = lambda: None
    return runner


_REAL_WHICH = shutil.which


def _which(name, *a, **k):
    if name == "setsid":
        return "/usr/bin/setsid"
    if name == "clover":
        return None
    return _REAL_WHICH(name, *a, **k)


_REAL_POPEN = subprocess.Popen


class _SpawnRecorder:
    """Stands in for subprocess.Popen: git probes run for real, anything else
    (the detached updater spawn) is only recorded."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, *a, **k):
        if isinstance(argv, (list, tuple)) and argv and Path(str(argv[0])).name.startswith("git"):
            return _REAL_POPEN(argv, *a, **k)
        self.calls.append((argv, k))
        return SimpleNamespace(pid=0)

    def assert_not_called(self):
        assert self.calls == [], f"update spawned: {self.calls!r}"

    def assert_called_once(self):
        assert len(self.calls) == 1, f"expected one spawn, got {self.calls!r}"


async def _send(runner, text, clover_home, action="update"):
    recorder = _SpawnRecorder()
    with patch("gateway.run._clover_home", clover_home), \
         patch("gateway.run._resolve_clover_bin", return_value=["/usr/bin/clover"]), \
         patch("shutil.which", side_effect=_which), \
         patch("clover_cli.update_contract.escape_gateway_cgroup", side_effect=lambda argv, env: (argv, env)), \
         patch("subprocess.Popen", recorder):
        if action == "update":
            reply = await runner._handle_update_command(_make_event(text))
        else:
            reply = await runner._handle_update_command(_make_event(text), action=action)
    return reply, recorder


@pytest.mark.asyncio
async def test_chat_block_sends_one_plain_message_and_spawns_nothing(isolated, tmp_path):
    _make_dirty_wrong_branch(isolated)
    before = _tree_digest(isolated)
    home = tmp_path / "home"
    home.mkdir()

    reply, popen = await _send(_make_runner(), "/update", home)

    assert reply.startswith("⚠️ Update not started — nothing was changed.")
    assert "fix/local-work" in reply and "Fix:" in reply and "parked_branch_dirty" in reply
    popen.assert_not_called()
    assert not (home / ".update_pending.json").exists()
    assert _tree_digest(isolated) == before


@pytest.mark.asyncio
async def test_chat_update_check_only_reports(isolated, tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    reply, popen = await _send(_make_runner(), "/update check", home)

    assert reply.startswith("🔎 Update pre-check: ready to update")
    popen.assert_not_called()
    assert not (home / ".update_pending.json").exists()


@pytest.mark.asyncio
async def test_chat_update_check_reports_a_block(isolated, tmp_path):
    _make_dirty_wrong_branch(isolated)
    home = tmp_path / "home"
    home.mkdir()

    reply, popen = await _send(_make_runner(), "/update check", home)

    assert "can't update right now" in reply and "fix/local-work" in reply
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_chat_all_clear_keeps_the_existing_reply_and_spawn(isolated, tmp_path):
    from agent.i18n import t

    home = tmp_path / "home"
    home.mkdir()

    reply, popen = await _send(_make_runner(), "/update", home)

    assert reply == t("gateway.update.starting")
    popen.assert_called_once()
    assert (home / ".update_pending.json").exists()


@pytest.mark.asyncio
async def test_chat_precheck_crash_falls_back_to_the_normal_update(isolated, tmp_path, monkeypatch):
    from agent.i18n import t

    def boom(**_kw):
        raise RuntimeError("pre-check broke")

    monkeypatch.setattr(pf, "run_update_preflight", boom)
    home = tmp_path / "home"
    home.mkdir()

    reply, popen = await _send(_make_runner(), "/update", home)

    assert reply == t("gateway.update.starting")
    popen.assert_called_once()


@pytest.mark.asyncio
async def test_chat_repair_is_not_prechecked(isolated, tmp_path, monkeypatch):
    _make_dirty_wrong_branch(isolated)
    monkeypatch.setattr(pf, "run_update_preflight", lambda **_kw: pytest.fail("repair ran the update pre-check"))
    home = tmp_path / "home"
    home.mkdir()
    _reply, popen = await _send(_make_runner(), "/repair", home, action="repair")
    popen.assert_called_once()

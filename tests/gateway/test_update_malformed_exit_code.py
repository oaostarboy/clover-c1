"""A malformed ``.update_exit_code`` must never swallow the /update completion message.

Real incident (Oracle, Raspberry Pi, systemd 257, 2026-10-07): after a successful
``/update`` the restarted gateway logged ``Update final notification failed:
invalid literal for int() with base 10: '1479546rc'`` and the "Now on Clover ...
What's new" message was never sent.

Root cause: the chat updater is wrapped as ``bash -c "...; rc=$?; printf '%s'
\"$rc\" > .update_exit_code"`` and launched through ``systemd-run --user --scope``.
``systemd_user_scope_argv`` doubled every ``$`` unconditionally because systemd 261
expands ``$`` in ``--scope`` arguments; systemd 257 does not, so bash received
``rc=$$?; printf '%s' "$$rc"`` -- ``$$`` is the shell's PID -- and wrote
``<pid>rc`` into the exit-code file.  The reader then did a bare ``int()``.

Two layers, both covered here:
  * writer: the wrapper reaches bash verbatim on BOTH systemd behaviours;
  * reader: a malformed value is logged and the completion message still goes out
    (and a genuinely failed update still reports failure).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clover_cli import release_notes as rn
from clover_cli import update_contract
from gateway.config import Platform

BAD = "1479546rc"  # the exact value from Oracle's log

NOTES = """\
## 1.1.1 | Clover C1.1.1 | 2026-10-07
- Fix A.
- Fix B.

## 1.1.0 | Clover C1.1 | 2026-10-07
- First named release.
"""

FAKE_SYSTEMD_RUN = """#!/bin/bash
# Stand-in for systemd-run --scope. Drops flags up to "--", then execs the rest.
# With a sibling "expand" file it mimics systemd 261 ($$ -> $ in arguments);
# without it, systemd 257 (arguments untouched).
here="$(dirname "$0")"
while [ "$1" != "--" ]; do shift; done; shift
if [ -f "$here/expand" ]; then
  args=(); for a in "$@"; do args+=("${a//\\$\\$/\\$}"); done
  exec "${args[@]}"
fi
exec "$@"
"""


def _runner():
    from gateway.run import GatewayRunner

    r = object.__new__(GatewayRunner)
    r.adapters = {}
    r._voice_mode = {}
    r._update_prompt_pending = {}
    r._running_agents = {}
    r._running_agents_ts = {}
    r._pending_messages = {}
    r._pending_approvals = {}
    r._failed_platforms = {}
    r.config = None
    r._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": False}}
    return r


def _sent(adapter) -> str:
    return "\n".join(str(c.args[1]) for c in adapter.send.call_args_list)


def _marker(home, **fields):
    p = home / ".update_pending.json"
    p.write_text(json.dumps({"platform": "telegram", "chat_id": "1", **fields}), encoding="utf-8")
    return p


def _receipt(home, outcome="success", started_offset=2):
    d = home / "logs" / "update_receipts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text(
        json.dumps({
            "started_at": (datetime.now(timezone.utc) + timedelta(seconds=started_offset)).isoformat(),
            "outcome": outcome,
            "pre_update": {"version": "1.1.0", "sha": "old"},
            "post_update": {"version": "1.1.1", "sha": "new", "short_sha": "new"},
        }),
        encoding="utf-8",
    )


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "clover"
    h.mkdir()
    notes = tmp_path / "RELEASE_NOTES.md"
    notes.write_text(NOTES, encoding="utf-8")
    monkeypatch.setattr(rn, "default_notes_path", lambda: notes)
    monkeypatch.setattr(rn, "current_code_identity", lambda: ("1.1.1", "new"))
    with patch("gateway.run._clover_home", h):
        yield h


# ---------------------------------------------------------------------------
# Reader: the exact value from Oracle's gateway.log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_watcher_sends_whats_new_when_exit_code_is_pid_glued_to_rc(home, caplog):
    runner, adapter = _runner(), AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    _marker(home, from_version="1.1.0", from_sha="old", session_key="agent:main:telegram:dm:1")
    _receipt(home, "success")
    (home / ".update_output.txt").write_text("→ Fetching updates...\n", encoding="utf-8")
    (home / ".update_exit_code").write_text(BAD, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        await runner._watch_update_progress(poll_interval=0.05, stream_interval=0.05, timeout=5.0)

    finals = [str(c.args[1]) for c in adapter.send.call_args_list if "update finished" in str(c.args[1]).lower()]
    assert len(finals) == 1, _sent(adapter)
    assert "Now on Clover C1.1.1" in finals[0] and "• Fix A." in finals[0]
    assert "failed" not in _sent(adapter).lower()
    assert BAD in caplog.text  # the bad value is logged, not hidden
    assert "Update final notification failed" not in caplog.text
    assert not (home / ".update_exit_code").exists()  # cleanup still ran


@pytest.mark.asyncio
async def test_legacy_notifier_sends_whats_new_when_exit_code_is_pid_glued_to_rc(home, caplog):
    runner, adapter = _runner(), AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    _marker(home, from_version="1.1.0", from_sha="old")
    _receipt(home, "success")
    (home / ".update_output.txt").write_text("✓ Code updated!", encoding="utf-8")
    (home / ".update_exit_code").write_text(BAD, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert await runner._send_update_notification() is True

    msg = _sent(adapter)
    assert msg.startswith("✅ Clover update finished.")
    assert "Now on Clover C1.1.1" in msg and "• Fix B." in msg
    assert BAD in caplog.text
    assert "Post-update notification failed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["failed", "partial", None])
async def test_malformed_exit_code_never_turns_a_failure_into_success(home, receipt):
    """Fail closed: only this run's *success* receipt may upgrade a bad value."""
    runner, adapter = _runner(), AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    _marker(home, from_version="1.1.0", from_sha="old", session_key="agent:main:telegram:dm:1")
    if receipt:
        _receipt(home, receipt)
    (home / ".update_exit_code").write_text(BAD, encoding="utf-8")

    await runner._watch_update_progress(poll_interval=0.05, stream_interval=0.05, timeout=5.0)

    assert "❌ Clover update failed (exit code 1)." in _sent(adapter)
    assert "Now on Clover" not in _sent(adapter)


@pytest.mark.asyncio
async def test_malformed_exit_code_ignores_a_success_receipt_from_an_earlier_run(home):
    runner, adapter = _runner(), AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    _marker(home, from_version="1.1.0", from_sha="old", session_key="agent:main:telegram:dm:1")
    _receipt(home, "success", started_offset=-86400)  # yesterday's update
    (home / ".update_exit_code").write_text(BAD, encoding="utf-8")

    await runner._watch_update_progress(poll_interval=0.05, stream_interval=0.05, timeout=5.0)

    assert "❌ Clover update failed (exit code 1)." in _sent(adapter)


@pytest.mark.parametrize("raw", ["", "  \n", "0", "1", "2", "124"])
def test_well_formed_exit_codes_are_unchanged(tmp_path, raw):
    expected = int(raw) if raw.strip() else 1
    assert update_contract.parse_update_exit_code(raw, tmp_path) == expected


@pytest.mark.parametrize("raw", [BAD, "rc", "0rc", "1234\n5678", "0x1", "\x00\x00"])
def test_parser_never_raises(tmp_path, raw):
    assert update_contract.parse_update_exit_code(raw, tmp_path) == 1


# ---------------------------------------------------------------------------
# Writer: the wrapper must reach bash verbatim on every systemd version
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_systemd_run(tmp_path, monkeypatch):
    d = tmp_path / "fakebin"
    d.mkdir()
    exe = d / "systemd-run"
    exe.write_text(FAKE_SYSTEMD_RUN, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(update_contract, "_scope_expands_dollar", None, raising=False)
    return d


def _run_wrapper(systemd_run: str) -> str:
    cmd = "false; rc=$?; printf '%s' \"$rc\""
    argv = update_contract.systemd_user_scope_argv(["bash", "-c", cmd], systemd_run=systemd_run)
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20).stdout


@pytest.mark.parametrize("expands", [False, True], ids=["systemd-257-no-expansion", "systemd-261-expands"])
def test_updater_wrapper_reports_the_real_exit_status(fake_systemd_run, expands):
    if expands:
        (fake_systemd_run / "expand").touch()
    assert _run_wrapper(str(fake_systemd_run / "systemd-run")) == "1"


@pytest.mark.live_system_guard_bypass  # runs a FAKE updater script, never the real `clover update`
@pytest.mark.asyncio
@pytest.mark.parametrize("expands", [False, True], ids=["systemd-257-no-expansion", "systemd-261-expands"])
async def test_chat_update_end_to_end_delivers_whats_new(home, tmp_path, fake_systemd_run, monkeypatch, expands):
    """Real path: /update builds the updater command, the (fake) systemd-run
    launches it, the shell writes .update_exit_code, the restarted gateway reads
    it and sends the completion message.  Nothing between is mocked."""
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource

    if expands:
        (fake_systemd_run / "expand").touch()
    # A harmless "clover" that succeeds, so the wrapper has a real exit status.
    fake_clover = fake_systemd_run / "fake-updater"
    fake_clover.write_text("#!/bin/bash\necho updated\nexit 0\n", encoding="utf-8")
    fake_clover.chmod(0o755)

    monkeypatch.setattr(update_contract, "_own_cgroup_path", lambda: (
        "/user.slice/user-1000.slice/user@1000.service/app.slice/clover-gateway.service"
    ), raising=False)
    monkeypatch.setattr(update_contract, "_scope_probe_result", None, raising=False)
    table = {"clover": str(fake_clover), "systemd-run": str(fake_systemd_run / "systemd-run"),
             "setsid": "/usr/bin/setsid"}
    real_popen = subprocess.Popen
    captured = {}

    def capture_popen(argv, *a, **kw):
        # Only the updater launch is captured; the scope/$-expansion probes
        # (subprocess.run -> Popen) run for real against the fake systemd-run.
        if "update_exit_code" not in " ".join(map(str, argv)):
            return real_popen(argv, *a, **kw)
        captured["argv"] = argv
        captured["kw"] = kw
        return MagicMock()

    fake_root = tmp_path / "project"
    (fake_root / "gateway").mkdir(parents=True)
    (fake_root / "gateway" / "run.py").touch()
    (fake_root / ".git").mkdir()

    runner = _runner()
    source = SessionSource(platform=Platform.TELEGRAM, user_id="1", chat_id="1",
                           user_name="u", thread_id=None)
    with patch("gateway.run.__file__", str(fake_root / "gateway" / "run.py")), \
         patch("shutil.which", side_effect=lambda n, *a, **k: table.get(n)), \
         patch("subprocess.Popen", capture_popen):
        reply = await runner._handle_update_command(MessageEvent(text="/update", source=source))
    assert "Starting Clover update" in reply
    assert "systemd-run" in captured["argv"][0]

    env = dict(captured["kw"]["env"])
    proc = real_popen(captured["argv"], env=env, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, start_new_session=True)
    proc.wait(timeout=30)
    deadline = time.time() + 10
    while not (home / ".update_exit_code").exists() and time.time() < deadline:
        time.sleep(0.1)
    assert (home / ".update_exit_code").read_text(encoding="utf-8") == "0"  # the writer is right

    # Reader: the gateway that comes back up after the restart. The marker was
    # written by the OLD (1.1.0) gateway, so it names 1.1.0 as the origin.
    _marker(home, from_version="1.1.0", from_sha="old")
    _receipt(home, "success")
    adapter = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    assert await runner._send_update_notification() is True
    assert "Now on Clover C1.1.1" in _sent(adapter)


@pytest.mark.linux_only
def test_real_systemd_run_delivers_the_wrapper_verbatim():
    """Whatever systemd-run this host has (257 and 261 differ), `$?` survives."""
    import shutil

    exe = shutil.which("systemd-run")
    if not exe:
        pytest.skip("systemd-run not installed")
    probe = update_contract.systemd_user_scope_argv(["true"], systemd_run=exe)
    try:
        usable = subprocess.run(probe, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        usable = False
    if not usable:
        pytest.skip("no systemd user manager reachable from this host")
    update_contract._scope_expands_dollar = None
    assert _run_wrapper(exe) == "1"

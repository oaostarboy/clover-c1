"""Chat ``/update`` must start the updater outside the gateway's cgroup.

The updater restarts the gateway's systemd unit.  A ``setsid`` child is a new
session but still lives in the unit's cgroup, so ``KillMode=mixed`` SIGKILLs
the updater (and the rollback watcher it starts) together with the old
gateway.  On Linux, under a *user* unit, the spawn is prefixed with
``systemd-run --user --scope`` so the updater lands in its own ``run-*.scope``.
"""

import json
import logging
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

SCOPE_PREFIX = ["--user", "--scope", "--quiet", "--collect", "--"]
USER_UNIT_CGROUP = (
    "/user.slice/user-1000.slice/user@1000.service/app.slice/clover-gateway.service"
)
SYSTEM_UNIT_CGROUP = "/system.slice/clover-gateway.service"
SESSION_CGROUP = "/user.slice/user-1000.slice/session-42.scope"


def _make_event():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="12345",
        chat_id="67890",
        user_name="testuser",
        thread_id=None,
    )
    return MessageEvent(text="/update", source=source)


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._update_prompt_pending = {}
    return runner


def _which(*, systemd_run="/usr/bin/systemd-run", setsid="/usr/bin/setsid"):
    table = {"clover": "/usr/bin/clover", "systemd-run": systemd_run, "setsid": setsid}
    return lambda name, *a, **kw: table.get(name)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated CLOVER_HOME plus a fresh scope-probe cache for every test."""
    from clover_cli import update_contract

    clover_home = tmp_path / "clover"
    clover_home.mkdir()
    fake_root = tmp_path / "project"
    (fake_root / ".git").mkdir(parents=True)
    (fake_root / "gateway").mkdir()
    (fake_root / "gateway" / "run.py").touch()
    # raising=False: the seams do not exist before the fix; the tests must
    # then fail on the argv assertions, not on a missing attribute.
    monkeypatch.setattr(update_contract, "_scope_probe_result", None, raising=False)
    return {
        "home": clover_home,
        "run_py": str(fake_root / "gateway" / "run.py"),
        "tmp": tmp_path,
    }


def _set_cgroup(monkeypatch, path):
    from clover_cli import update_contract

    monkeypatch.setattr(update_contract, "_own_cgroup_path", lambda: path, raising=False)


async def _run_update(env, *, which, probe=None):
    """Run the chat /update handler; return (popen mock, probe mock, reply)."""
    runner = _make_runner()
    popen = MagicMock()
    probe = probe or MagicMock(return_value=subprocess.CompletedProcess([], 0))
    with patch("gateway.run._clover_home", env["home"]), \
         patch("gateway.run.__file__", env["run_py"]), \
         patch("shutil.which", side_effect=which), \
         patch("subprocess.run", probe), \
         patch("subprocess.Popen", popen):
        reply = await runner._handle_update_command(_make_event())
    return popen, probe, reply


@pytest.mark.linux_only
class TestLinuxUserUnitEscapesCgroup:
    @pytest.mark.asyncio
    async def test_argv_is_prefixed_and_command_unchanged(self, env, monkeypatch):
        _set_cgroup(monkeypatch, USER_UNIT_CGROUP)
        popen, probe, reply = await _run_update(env, which=_which())

        argv = popen.call_args[0][0]
        assert argv[0] == "/usr/bin/systemd-run"
        assert argv[1:6] == SCOPE_PREFIX
        assert popen.call_count == 1
        assert popen.call_args.kwargs["start_new_session"] is True
        assert "Starting Clover update" in reply

        # What follows is today's command.  systemd-run un-escapes ``$$``, so
        # the program receives it once each ``$$`` is read back as ``$``.
        _set_cgroup(monkeypatch, SYSTEM_UNIT_CGROUP)
        today, _, _ = await _run_update(env, which=_which())
        today_argv = today.call_args[0][0]
        assert today_argv[:3] == ["/usr/bin/setsid", "bash", "-c"]
        assert [a.replace("$$", "$") for a in argv[6:]] == today_argv
        assert ".update_exit_code" in argv[9] and "--gateway" in argv[9]

    @pytest.mark.asyncio
    async def test_no_setsid_binary_still_prefixes_plain_bash(self, env, monkeypatch):
        _set_cgroup(monkeypatch, USER_UNIT_CGROUP)
        popen, _, _ = await _run_update(env, which=_which(setsid=None))

        argv = popen.call_args[0][0]
        assert argv[:6] == ["/usr/bin/systemd-run", *SCOPE_PREFIX]
        assert argv[6:8] == ["bash", "-c"]

    @pytest.mark.asyncio
    async def test_user_bus_env_survives_for_systemd_run(self, env, monkeypatch):
        runtime = env["tmp"] / "run-user"
        runtime.mkdir()
        (runtime / "bus").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        _set_cgroup(monkeypatch, USER_UNIT_CGROUP)
        popen, probe, _ = await _run_update(env, which=_which())

        spawn_env = popen.call_args.kwargs["env"]
        assert spawn_env["XDG_RUNTIME_DIR"] == str(runtime)
        assert spawn_env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime / 'bus'}"
        # The probe runs with the same env the real spawn gets.
        assert probe.call_args.kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == spawn_env[
            "DBUS_SESSION_BUS_ADDRESS"
        ]

    @pytest.mark.asyncio
    async def test_probe_runs_the_same_scope_with_true_and_is_cached(self, env, monkeypatch):
        _set_cgroup(monkeypatch, USER_UNIT_CGROUP)
        probe = MagicMock(return_value=subprocess.CompletedProcess([], 0))
        await _run_update(env, which=_which(), probe=probe)
        await _run_update(env, which=_which(), probe=probe)

        # The updater wrapper contains ``$?``, so the builder also asks the
        # installed systemd-run whether it expands ``$`` (257 does not, 261
        # does); that is a different probe. Only the ``true`` pre-check counts.
        scope_checks = [c for c in probe.call_args_list if c[0][0][-1] == "true"]
        assert len(scope_checks) == 1, "the scope pre-check is cached per process"
        probe_argv = scope_checks[0][0][0]
        assert probe_argv == ["/usr/bin/systemd-run", *SCOPE_PREFIX, "true"]
        assert scope_checks[0].kwargs.get("timeout"), "the pre-check must be bounded"

    @pytest.mark.asyncio
    async def test_missing_systemd_run_falls_back_and_warns(self, env, monkeypatch, caplog):
        _set_cgroup(monkeypatch, USER_UNIT_CGROUP)
        with caplog.at_level(logging.WARNING):
            popen, probe, reply = await _run_update(env, which=_which(systemd_run=None))

        argv = popen.call_args[0][0]
        assert argv[0] == "/usr/bin/setsid" and argv[1:3] == ["bash", "-c"]
        assert popen.call_count == 1
        probe.assert_not_called()
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("systemd-run" in r.getMessage() for r in warnings)
        assert "Starting Clover update" in reply

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "probe",
        [
            MagicMock(return_value=subprocess.CompletedProcess([], 1)),
            MagicMock(side_effect=subprocess.TimeoutExpired("systemd-run", 10)),
            MagicMock(side_effect=OSError("exec failed")),
        ],
        ids=["nonzero-exit", "timeout", "oserror"],
    )
    async def test_failed_scope_probe_falls_back_with_one_updater_launch(
        self, env, monkeypatch, caplog, probe
    ):
        _set_cgroup(monkeypatch, USER_UNIT_CGROUP)
        with caplog.at_level(logging.WARNING):
            popen, _, reply = await _run_update(env, which=_which(), probe=probe)

        assert popen.call_count == 1, "the updater must never be launched twice"
        argv = popen.call_args[0][0]
        assert "systemd-run" not in " ".join(argv[:3])
        assert argv[0] == "/usr/bin/setsid" and argv[1:3] == ["bash", "-c"]
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("systemd-run" in r.getMessage() for r in warnings)
        assert "Starting Clover update" in reply


@pytest.mark.linux_only
class TestLinuxOtherScopesUnchanged:
    @pytest.mark.asyncio
    async def test_system_scope_unit_keeps_plain_setsid(self, env, monkeypatch):
        _set_cgroup(monkeypatch, SYSTEM_UNIT_CGROUP)
        popen, probe, _ = await _run_update(env, which=_which())

        argv = popen.call_args[0][0]
        assert argv[0] == "/usr/bin/setsid" and argv[1:3] == ["bash", "-c"]
        assert len(argv) == 4
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_in_any_unit_keeps_plain_setsid(self, env, monkeypatch):
        # A gateway started from a login shell lives in a session scope, not a
        # service; there is no unit for KillMode to act on.
        _set_cgroup(monkeypatch, SESSION_CGROUP)
        popen, probe, _ = await _run_update(env, which=_which())

        assert popen.call_args[0][0][0] == "/usr/bin/setsid"
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreadable_cgroup_keeps_plain_setsid(self, env, monkeypatch):
        _set_cgroup(monkeypatch, None)
        popen, probe, _ = await _run_update(env, which=_which())

        assert popen.call_args[0][0][0] == "/usr/bin/setsid"
        probe.assert_not_called()


class TestSharedScopeArgv:
    """The /update spawn and the restart-recovery spawn build one argv."""

    def test_builder_prefixes_command(self):
        from clover_cli.update_contract import systemd_user_scope_argv

        argv = systemd_user_scope_argv(["python", "-m", "x"], systemd_run="/bin/systemd-run")
        assert argv == ["/bin/systemd-run", *SCOPE_PREFIX, "python", "-m", "x"]

    def test_builder_returns_none_without_systemd_run(self):
        from clover_cli.update_contract import systemd_user_scope_argv

        with patch("shutil.which", return_value=None):
            assert systemd_user_scope_argv(["true"]) is None


@pytest.mark.linux_only
def test_scope_delivers_every_argument_to_the_program_as_written():
    """systemd-run expands ``$`` in its arguments (``$$``, ``${VAR}``, a
    whole-word ``$VAR``/``$?``); the program must still receive exactly what
    the caller wrote.  Real systemd-run, no mocks."""
    from clover_cli.update_contract import systemd_user_scope_argv

    if not shutil.which("systemd-run"):
        pytest.skip("systemd-run not installed")
    probe = systemd_user_scope_argv(["true"])
    try:
        usable = subprocess.run(probe, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        usable = False
    if not usable:
        pytest.skip("no systemd user manager reachable from this host")

    wanted = ["$HOME", "${HOME}", "$?", "$NO_SUCH_VAR_XYZ", "a$$b", "$$$", "rc=$?; printf '%s' \"$rc\""]
    code = "import json, sys; print(json.dumps(sys.argv[1:]))"
    argv = systemd_user_scope_argv(["python3", "-c", code, *wanted])
    out = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    assert json.loads(out.stdout) == wanted, out.stderr


@pytest.mark.windows_only
class TestWindowsSpawnUnchanged:
    @pytest.mark.asyncio
    async def test_no_systemd_run_anywhere(self, env):
        popen, probe, _ = await _run_update(env, which=_which())

        argv = popen.call_args[0][0]
        assert "systemd-run" not in " ".join(argv)
        probe.assert_not_called()
        assert "start_new_session" not in popen.call_args.kwargs


@pytest.mark.macos_only
class TestMacosSpawnUnchanged:
    @pytest.mark.asyncio
    async def test_no_systemd_run_anywhere(self, env):
        popen, probe, _ = await _run_update(env, which=_which())

        argv = popen.call_args[0][0]
        assert argv[0] == "/usr/bin/setsid" and argv[1:3] == ["bash", "-c"]
        probe.assert_not_called()

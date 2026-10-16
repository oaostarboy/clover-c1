"""Behavior contracts for the shared test-only live-system guard."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests" / "clover_cli" / "_inert_venv_holder.py"
GATEWAY = ["-m", "clover_cli.main", "gateway", "run"]


def _inert_command(mode="sleep", tail=None):
    return [sys.executable, "-I", "-B", str(HELPER), mode, *(GATEWAY if tail is None else tail)]


def _inert_options() -> dict[str, Any]:
    return {
        "cwd": str(ROOT),
        "env": {k: os.environ[k] for k in (
            "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP",
            "TMPDIR", "HOME", "USERPROFILE", "LANG", "TZ", "CLOVER_HOME",
            "CLOVER_TEST_ISOLATION",
        ) if k in os.environ},
        "shell": False,
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }


def test_fixed_inert_gateway_is_admitted_and_really_classified():
    """No marker, raw launcher, platform patch, or classifier mock."""
    from clover_cli.update_cmd import _classify_concurrent_instance

    proc = subprocess.Popen(_inert_command(), **_inert_options())
    try:
        assert proc.poll() is None
        assert _classify_concurrent_instance(proc.pid) == "gateway"
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert proc.returncode is not None


def test_fixed_parent_scan_has_real_gateway_ancestry_and_detector():
    import json

    result = subprocess.run(_inert_command("parent-scan"), timeout=20, **_inert_options())
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["gateway_ancestors"]
    assert all(row["classification"] == "gateway" for row in payload["ancestors"]
               if row["pid"] in payload["gateway_ancestors"])
    assert payload["pid"] not in payload["gateway_ancestors"]
    if sys.platform == "win32":
        assert set(payload["gateway_ancestors"]) & set(payload["pids"])
    else:
        # The real Windows detector fails closed off-Windows. Not native proof.
        assert payload["pids"] == []


@pytest.fixture
def no_os_exec(monkeypatch):
    """Negative probes must fail at the REAL guard, never reach OS execution."""
    def unexpected_exec(*args, **kwargs):
        pytest.fail("guard allowed forbidden argv to reach OS execution")
    monkeypatch.setattr(subprocess.Popen, "_execute_child", unexpected_exec)


@pytest.mark.parametrize("change", [
    "unknown-helper", "unknown-mode", "unknown-tail", "injected-replace",
    "code-entry", "module-entry", "cli-entry", "raw-code", "raw-module",
    "raw-cli", "raw-replace", "wrapper", "string", "tuple", "python-alias",
    "no-isolation", "no-bytecode-flag", "extra-arg", "scan-tail",
])
def test_only_fixed_argv_grammar_is_admitted(change, tmp_path, no_os_exec):
    cmd = _inert_command()
    if change == "unknown-helper":
        cmd[3] = str(tmp_path / "other.py")
    elif change == "unknown-mode":
        cmd[4] = "exec"
    elif change == "unknown-tail":
        cmd.append("extra")
    elif change == "injected-replace":
        cmd.append("--replace")
    elif change == "code-entry":
        cmd[1:4] = ["-c", "pass", str(HELPER)]
    elif change == "module-entry":
        cmd[1:4] = ["-m", "clover_cli.main", str(HELPER)]
    elif change == "cli-entry":
        cmd[0] = "clover"
    elif change == "raw-code":
        cmd = [sys.executable, "-c", "pass", *GATEWAY]
    elif change == "raw-module":
        cmd = [sys.executable, *GATEWAY]
    elif change == "raw-cli":
        cmd = ["clover", "gateway", "run"]
    elif change == "raw-replace":
        cmd = ["clover", "gateway", "run", "--replace"]
    elif change == "wrapper":
        cmd = ["env", *cmd]
    elif change == "string":
        cmd = " ".join(cmd)
    elif change == "tuple":
        cmd = tuple(cmd)
    elif change == "python-alias":
        cmd[0] = str(Path(sys.executable).parent / "." / "alias-python")
    elif change == "no-isolation":
        cmd.remove("-I")
    elif change == "no-bytecode-flag":
        cmd.remove("-B")
    elif change == "extra-arg":
        cmd.insert(4, "other.py")
    elif change == "scan-tail":
        cmd[4] = "scan"
    with pytest.raises(AssertionError, match="live-system guard|REAL gateway"):
        subprocess.Popen(cmd, **_inert_options())


@pytest.mark.parametrize("override", [
    {"shell": True}, {"executable": sys.executable}, {"executable": None},
    {"start_new_session": True}, {"start_new_session": False},
    {"creationflags": 8}, {"creationflags": 0}, {"startupinfo": None},
    {"preexec_fn": lambda: None}, {"process_group": 0}, {"close_fds": False},
    {"env": None}, {"cwd": None},
])
def test_process_control_overrides_remain_denied(override, no_os_exec):
    options = _inert_options() | override
    with pytest.raises(AssertionError, match="live-system guard"):
        subprocess.Popen(_inert_command(), **options)


def test_positional_process_control_override_is_denied(no_os_exec):
    with pytest.raises(AssertionError, match="live-system guard"):
        subprocess.Popen(_inert_command(), 0, **_inert_options())


@pytest.mark.parametrize("key", ["PYTHONPATH", "LD_PRELOAD", "HOME", "CLOVER_HOME"])
def test_environment_injection_is_denied(key, tmp_path, no_os_exec):
    options = _inert_options()
    options["env"][key] = str(tmp_path)
    with pytest.raises(AssertionError, match="live-system guard"):
        subprocess.Popen(_inert_command(), **options)


def test_unknown_cwd_is_denied(tmp_path, no_os_exec):
    options = _inert_options()
    options["cwd"] = str(tmp_path)
    with pytest.raises(AssertionError, match="live-system guard"):
        subprocess.Popen(_inert_command(), **options)


def test_mutated_helper_is_denied(no_os_exec):
    original = HELPER.read_bytes()
    try:
        HELPER.write_bytes(original + b"\n# changed bytes\n")
        with pytest.raises(AssertionError, match="live-system guard"):
            subprocess.Popen(_inert_command(), **_inert_options())
    finally:
        HELPER.write_bytes(original)


@pytest.mark.linux_only
def test_symlink_substitution_is_denied(no_os_exec):
    backup = HELPER.with_suffix(".reviewed")
    HELPER.rename(backup)
    try:
        HELPER.symlink_to(backup)
        with pytest.raises(AssertionError, match="live-system guard"):
            subprocess.Popen(_inert_command(), **_inert_options())
    finally:
        HELPER.unlink(missing_ok=True)
        backup.rename(HELPER)


@pytest.mark.linux_only
@pytest.mark.parametrize("kind", ["python", "helper"])
def test_same_content_symlink_alias_is_denied(kind, tmp_path, no_os_exec):
    alias = tmp_path / "alias"
    alias.symlink_to(sys.executable if kind == "python" else HELPER)
    cmd = _inert_command()
    cmd[0 if kind == "python" else 3] = str(alias)
    with pytest.raises(AssertionError, match="live-system guard|REAL gateway"):
        subprocess.Popen(cmd, **_inert_options())


def test_reviewed_crlf_checkout_bytes_are_admitted():
    original = HELPER.read_bytes()
    try:
        HELPER.write_bytes(original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        result = subprocess.run(_inert_command("scan", []), timeout=20, **_inert_options())
        assert result.returncode == 0, result.stderr
    finally:
        HELPER.write_bytes(original)


@pytest.mark.parametrize("cmd", [
    ["systemctl", "restart", "clover-gateway"],
    ["clover", "update"], [sys.executable, "-m", "clover_cli.main", "update"],
])
def test_other_dangerous_rules_remain_in_force(cmd, no_os_exec):
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(cmd)


def test_spawn_setup_failure_reaps_real_child(monkeypatch):
    import psutil
    from tests.clover_cli import test_venv_holder_windows_live as live

    before = {p.pid for p in psutil.Process().children(recursive=True)}
    def fail_readiness(_seconds):
        raise AssertionError("injected setup failure")
    from types import SimpleNamespace
    monkeypatch.setattr(live, "time", SimpleNamespace(sleep=fail_readiness))
    try:
        with pytest.raises(AssertionError, match="injected setup failure"):
            live._spawn(["-m", "clover_cli.main", "serve"])
        assert not live._SPAWNED
        assert not ({p.pid for p in psutil.Process().children(recursive=True)} - before)
    finally:
        # Keep the RED probe safe as well: reap only this probe's own children.
        for proc in psutil.Process().children(recursive=True):
            if proc.pid not in before:
                proc.kill()
                proc.wait(timeout=10)
        live._SPAWNED.clear()


@pytest.fixture(autouse=True)
def no_child_residue():
    import psutil
    from tests.clover_cli import test_venv_holder_windows_live as live

    before = {p.pid for p in psutil.Process().children(recursive=True)}
    yield
    live._kill(*list(live._SPAWNED))
    assert not ({p.pid for p in psutil.Process().children(recursive=True)} - before)


@pytest.mark.parametrize("method", [
    "test_live_gateway_process_classified_gateway",
    "test_live_non_gateway_processes_keep_the_abort",
    "test_live_filter_drops_only_the_gateway",
])
def test_portable_original_real_classifier_bodies(method):
    from tests.clover_cli import test_venv_holder_windows_live as live

    # These bodies need only real psutil/classifiers, unlike native detection.
    getattr(live.TestConcurrentGateClassification(), method)()


@pytest.mark.parametrize("letter", ["x", "y"])
def test_long_gateway_data_is_untruncated_and_pausable(letter):
    import psutil
    from clover_cli.update_cmd import (
        _classify_concurrent_instance, _leftover_pausable_gateway_pids,
    )
    from tests.clover_cli import test_venv_holder_windows_live as live

    padding = 'C:\\Users\\' + letter * 90 + '\\.clover-runtime'
    proc = live._spawn([padding, *GATEWAY])
    try:
        cmdline = " ".join(psutil.Process(proc.pid).cmdline())
        assert cmdline.index("gateway run") > 120
        assert _classify_concurrent_instance(proc.pid) == "gateway"
        assert _leftover_pausable_gateway_pids([(proc.pid, "python", cmdline)]) == [proc.pid]
    finally:
        live._kill(proc)


@pytest.mark.parametrize("tail", [
    ["-m", "clover_cli.main", "serve"],
    ["-m", "clover_cli.main", "dashboard"],
    ["-m", "clover_cli.main", "kanban", "--preserve-cache"],
    ["totally", "unrelated"],
])
def test_fixed_non_gateway_tails_really_classify_non_gateway(tail):
    from clover_cli.update_cmd import _classify_concurrent_instance
    from tests.clover_cli import test_venv_holder_windows_live as live

    proc = live._spawn(tail)
    try:
        assert _classify_concurrent_instance(proc.pid) == "non-gateway"
    finally:
        live._kill(proc)


def test_sleep_has_a_finite_deadline_without_cleanup():
    result = subprocess.run(_inert_command(), timeout=25, **_inert_options())
    assert result.returncode == 0, result.stderr


def test_foreign_sleep_cwd_cannot_inject_imports(tmp_path):
    from clover_cli.update_cmd import _classify_concurrent_instance
    from tests.clover_cli import test_venv_holder_windows_live as live

    # -I must ignore hostile import names in an outside working directory.
    (tmp_path / "time.py").write_text("raise RuntimeError('cwd injection')\n")
    (tmp_path / "sitecustomize.py").write_text("raise RuntimeError('cwd injection')\n")
    proc = live._spawn(["totally", "unrelated"], cwd=tmp_path.resolve())
    try:
        assert proc.poll() is None
        assert _classify_concurrent_instance(proc.pid) == "non-gateway"
    finally:
        live._kill(proc)


def test_second_spawn_failure_is_reaped_by_fixture_finalizer():
    from tests.clover_cli import test_venv_holder_windows_live as live

    cleanup = getattr(live._cleanup_inert_processes, "__wrapped__")()
    next(cleanup)
    proc = live._spawn(GATEWAY)
    try:
        with pytest.raises(AssertionError, match="live-system guard"):
            live._spawn([*GATEWAY, "--replace"])
    finally:
        with pytest.raises(StopIteration):
            next(cleanup)
    assert proc.returncode is not None
    assert not live._SPAWNED


@pytest.mark.parametrize("entry", ["run", "call", "check_call", "check_output", "async-exec"])
def test_overrides_rejected_at_each_subprocess_entry(entry, no_os_exec):
    options = _inert_options()
    options["executable"] = sys.executable
    with pytest.raises(AssertionError, match="live-system guard"):
        if entry == "async-exec":
            import asyncio
            asyncio.run(asyncio.create_subprocess_exec(*_inert_command(), **options))
        else:
            getattr(subprocess, entry)(_inert_command(), **options)


def test_argv_arguments_are_not_treated_as_executables(tmp_path):
    target = tmp_path / "skill"
    target.write_text("just a filename\n")
    result = subprocess.run(["cat", str(target)], capture_output=True, text=True)
    assert result.returncode == 0
    assert "just a filename" in result.stdout


def test_direct_killer_argv_is_still_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "clover-guard-regression-nomatch"])


def test_wrapped_killer_command_is_still_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["bash", "-c", "pkill -f clover-guard-regression-nomatch"])


def test_env_wrapped_killer_command_is_still_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["env", "GUARD_TEST=1", "pkill", "-f", "clover-guard-regression-nomatch"])

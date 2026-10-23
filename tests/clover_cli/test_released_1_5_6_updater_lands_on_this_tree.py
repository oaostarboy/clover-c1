"""The REAL released v1.5.6 updater must land a running 1.5.6 gateway on this tree.

Most Clover users run 1.5.6. Their plain ``/update`` executes the 1.5.6
``update_cmd`` (frozen, already loaded), which pulls the new files and then
imports NEW modules (``managed_uv`` -> ``update_delivery_guard``, then the new
``clover_cli.gateway`` restart helpers). Unit tests with fakes cannot see an
incompatibility between those two halves, so this test runs the actual
released code against the actual tree under test:

* a hermetic CLOVER_HOME, a local bare "origin" (no network, not GitHub);
* the v1.5.6 source tree, checked out from git history, as the installed
  checkout whose ``clover update --gateway`` is executed;
* a real 1.5.6 gateway process running and idle (no capability flag);
* origin ``main`` = one commit whose tree is EXACTLY this repository's HEAD
  tree, parented on the v1.5.6 commit so the released updater fast-forwards.

Assertions: exit 0, no refusal and no "nothing was changed" text, a
``legacy-proceed`` receipt, the old gateway is replaced by one running the new
code that advertises the capability, no stale restart markers, no rollback.

Skipped when the v1.5.6 commit is unavailable (shallow clone without tags) --
the dedicated CI step fetches it. POSIX only: the Windows first transition is
exercised by the native workflow, not here.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX fixture; Windows runs the native workflow"),
    pytest.mark.live_system_guard_bypass,
]

REPO = Path(__file__).resolve().parents[2]
V156_TAG = "v1.5.6"
V156_SHA = "32d3e6728c71d1f7c8684b7b41ac1785cb8117a9"
GATEWAY_START_TIMEOUT = 90
UPDATE_TIMEOUT = 420
SETTLE_TIMEOUT = 150


def _git(*args, cwd=None, env=None, check=True):
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=False, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
    return result.stdout.strip()


def _have_v156() -> bool:
    probe = subprocess.run(["git", "cat-file", "-e", f"{V156_SHA}^{{commit}}"], cwd=REPO,
                           capture_output=True)
    if probe.returncode == 0:
        return True
    fetch = subprocess.run(["git", "fetch", "--no-tags", "--depth=1", "origin", V156_SHA],
                           cwd=REPO, capture_output=True)
    return fetch.returncode == 0 and subprocess.run(
        ["git", "cat-file", "-e", f"{V156_SHA}^{{commit}}"], cwd=REPO, capture_output=True
    ).returncode == 0


def _processes_for_home(home: Path):
    import psutil

    found = []
    for proc in psutil.process_iter(["pid"]):
        try:
            if proc.environ().get("CLOVER_HOME") == str(home):
                found.append(proc)
        except (psutil.Error, OSError):
            continue
    return found


def _kill_everything(home: Path):
    import psutil

    for _ in range(3):
        procs = _processes_for_home(home)
        if not procs:
            return
        for proc in procs:
            try:
                proc.send_signal(signal.SIGKILL)
            except (psutil.Error, OSError):
                pass
        time.sleep(0.5)


def _read_state(home: Path) -> dict:
    try:
        return json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _make_editable_venv(install: Path) -> Path:
    """<install>/venv with an editable-style clover-c1 record, like a real install.

    Dependencies come from the interpreter running the test as plain sys.path
    lines (no .pth processing, so the suite's own editable finder for the
    checkout under test cannot shadow the 1.5.6 tree).
    """
    import sysconfig

    venv = install / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True,
                   capture_output=True)
    python = venv / "bin" / "python"
    site = Path(subprocess.run(
        [str(python), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True).stdout.strip())
    deps = []
    for key in ("purelib", "platlib"):
        path = sysconfig.get_paths()[key]
        if path not in deps:
            deps.append(path)
    (site / "_test_deps.pth").write_text("\n".join([str(install), *deps]) + "\n", encoding="utf-8")
    dist = site / "clover_c1-1.5.6.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: clover-c1\nVersion: 1.5.6\n", encoding="utf-8")
    (dist / "direct_url.json").write_text(
        json.dumps({"url": install.as_uri(), "dir_info": {"editable": True}}), encoding="utf-8")
    (dist / "INSTALLER").write_text("test\n", encoding="utf-8")
    (dist / "RECORD").write_text("", encoding="utf-8")
    return python


def _shim_bin(tmp: Path, python: Path) -> Path:
    """A PATH containing only what the updater needs: no node/npm, no real uv."""
    shim = tmp / "shim-bin"
    shim.mkdir()
    for tool in ("git", "bash", "sh", "env", "ps", "kill", "sleep", "setsid", "uname",
                 "cat", "rm", "mkdir", "cp", "mv", "ls", "ln", "dirname", "basename",
                 "readlink", "chmod", "tr", "grep", "sed", "head", "tail", "id", "date"):
        real = shutil.which(tool)
        if real:
            (shim / tool).symlink_to(real)
    # The updater's Node/web steps are unrelated to the restart contract under
    # test and need network + minutes; an offline no-op npm keeps them out.
    npm = shim / "npm"
    npm.write_text("#!/bin/sh\necho \"fake npm $*\"\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)
    (shim / "python").symlink_to(python)
    (shim / "python3").symlink_to(python)
    return shim


FAKE_UV = """#!/bin/sh
# Offline stand-in for uv: every subcommand succeeds, nothing is downloaded.
case "$1" in
  --version) echo "uv 0.0.0-test" ;;
  pip) shift; echo "fake uv pip $*" ;;
  *) echo "fake uv $*" ;;
esac
exit 0
"""


@pytest.fixture
def legacy_install(tmp_path):
    if not _have_v156():
        pytest.skip(f"{V156_TAG} ({V156_SHA[:8]}) is not available in this clone")

    home = tmp_path / "home" / ".clover"
    (home / "bin").mkdir(parents=True)
    uv = home / "bin" / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    (home / ".env").write_text("", encoding="utf-8")

    # 1. origin: bare repo whose main is a commit with THIS tree on top of v1.5.6.
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(origin))
    _git("fetch", "--no-tags", "-q", str(REPO), V156_SHA, cwd=origin)
    _git("fetch", "--no-tags", "-q", str(REPO), "HEAD", cwd=origin)
    head_tree = _git("rev-parse", "FETCH_HEAD^{tree}", cwd=origin)
    ident = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    candidate = _git("commit-tree", head_tree, "-p", V156_SHA, "-m", "candidate tree", cwd=origin, env=ident)
    _git("update-ref", "refs/heads/main", candidate, cwd=origin)

    # 2. the user's 1.5.6 checkout, with origin pointing at the fixture.
    install = tmp_path / "clover-c1"
    install.mkdir()
    _git("init", "-q", "-b", "main", cwd=install)
    _git("fetch", "--no-tags", "-q", str(origin), V156_SHA, cwd=install)
    _git("checkout", "-q", "-B", "main", V156_SHA, cwd=install)
    _git("remote", "add", "origin", str(origin), cwd=install)
    _git("fetch", "--no-tags", "-q", "origin", "main", cwd=install)
    _git("branch", "--set-upstream-to=origin/main", "main", cwd=install)
    assert _git("rev-parse", "HEAD", cwd=install) == V156_SHA
    assert (install / "clover_cli" / "update_cmd.py").read_text(encoding="utf-8").rstrip() == \
        _git("show", f"{V156_SHA}:clover_cli/update_cmd.py", cwd=REPO).rstrip(), "not the released updater"

    venv_python = _make_editable_venv(install)
    shim = _shim_bin(tmp_path, venv_python)
    env = {
        "PATH": f"{shim}",
        "HOME": str(tmp_path / "home"),
        "CLOVER_HOME": str(home),
        "CLOVER_INSTALL_DIR": str(install),
        "VIRTUAL_ENV": str(install / "venv"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "NO_COLOR": "1",
        # The 1.5.6 child processes carry the suite's own live-database guard;
        # this CLOVER_HOME is a throwaway directory, so the bypass is correct.
        "CLOVER_STATE_DB_GUARD_BYPASS": "1",
    }
    # No systemctl on PATH and no XDG_RUNTIME_DIR: the updater takes the
    # manual-gateway path (as on launchd-less hosts) and cannot reach, let alone
    # restart, any real user unit of the machine running the tests.
    for keep in ("SYSTEMROOT", "TMPDIR", "LD_LIBRARY_PATH", "VIRTUAL_ENV_DISABLE_PROMPT"):
        if keep in os.environ:
            env[keep] = os.environ[keep]
    fixture = SimpleNamespace(home=home, install=install, env=env, origin=origin,
                              candidate=candidate, tmp=tmp_path)
    yield fixture
    _kill_everything(home)


def _start_legacy_gateway(fx):
    log = (fx.tmp / "gateway-old.log").open("w")
    proc = subprocess.Popen(
        [str(fx.install / "venv" / "bin" / "python"), "-m", "clover_cli.main", "gateway", "run"],
        cwd=fx.install, env=fx.env, stdin=subprocess.DEVNULL, stdout=log,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    deadline = time.monotonic() + GATEWAY_START_TIMEOUT
    while time.monotonic() < deadline:
        state = _read_state(fx.home)
        if state.get("gateway_state") == "running" and state.get("pid") == proc.pid:
            return proc, state
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    pytest.fail("1.5.6 gateway did not start:\n" + (fx.tmp / "gateway-old.log").read_text(errors="replace")[-3000:])


def test_released_1_5_6_updater_lands_a_running_gateway_on_this_tree(legacy_install):
    fx = legacy_install
    old_gateway, old_state = _start_legacy_gateway(fx)
    assert old_state["code_sha"] == V156_SHA
    assert "update_restart_capability" not in old_state, "fixture gateway must be a true 1.5.6 gateway"

    result = subprocess.run(
        [str(fx.install / "venv" / "bin" / "python"), "-m", "clover_cli.main", "update", "--yes", "--gateway"],
        cwd=fx.install, env=fx.env, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=UPDATE_TIMEOUT,
    )
    output = result.stdout + result.stderr
    (fx.tmp / "update-output.txt").write_text(output, encoding="utf-8")
    lowered = output.lower()

    assert result.returncode == 0, f"released updater exited {result.returncode}:\n{output[-4000:]}"
    assert "nothing was changed" not in lowered
    assert "automatic update deferred" not in lowered
    assert "legacy-gateway-maintenance" not in lowered
    assert "update complete" in lowered or "code updated" in lowered
    # The pull really happened with the released code driving it.
    assert _git("rev-parse", "HEAD", cwd=fx.install) == fx.candidate
    assert _git("rev-parse", "HEAD^{tree}", cwd=fx.install) == _git("rev-parse", "HEAD^{tree}", cwd=REPO)

    receipt = json.loads((fx.home / "logs" / "update_receipts" / "latest.json").read_text(encoding="utf-8"))
    assert receipt["outcome"] == "success"
    assert receipt["delivery_restart_guard"]["status"] == "legacy-proceed"

    # The legacy gateway was restarted onto the new code and says so.
    deadline = time.monotonic() + SETTLE_TIMEOUT
    state = {}
    while time.monotonic() < deadline:
        state = _read_state(fx.home)
        if (state.get("pid") not in (None, old_gateway.pid) and state.get("code_sha") == fx.candidate
                and state.get("gateway_state") == "running"):
            break
        time.sleep(1)
    else:
        pytest.fail(f"gateway never came back on the new code: {state}\n{output[-3000:]}")
    assert old_gateway.poll() is not None or not _pid_alive(old_gateway.pid)
    assert state["update_restart_capability"] == "reply-carriers-through-ack-v1"
    assert state.get("restart_requested") in (False, None)

    # No stale markers that would make a retry refuse or loop, and no rollback.
    for stale in ("fleet_restart_pending", ".update-incomplete"):
        assert not (fx.home / stale).exists(), stale
    beacon_deadline = time.monotonic() + SETTLE_TIMEOUT
    beacon = fx.home / ".clover-update-heartbeat.json"
    while beacon.exists() and time.monotonic() < beacon_deadline:
        time.sleep(1)
    assert not beacon.exists(), "the frozen restart watcher never stood down"
    assert _git("rev-parse", "HEAD", cwd=fx.install) == fx.candidate, "watcher rolled the update back"
    assert _read_state(fx.home).get("pid") == state["pid"], "gateway restarted a second time"


def test_second_update_after_landing_is_a_clean_no_op(legacy_install):
    """After the first transition a retry must not refuse or change anything."""
    fx = legacy_install
    _start_legacy_gateway(fx)
    first = subprocess.run(
        [str(fx.install / "venv" / "bin" / "python"), "-m", "clover_cli.main", "update", "--yes", "--gateway"],
        cwd=fx.install, env=fx.env, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=UPDATE_TIMEOUT)
    assert first.returncode == 0, (first.stdout + first.stderr)[-3000:]
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        state = _read_state(fx.home)
        if state.get("code_sha") == fx.candidate and state.get("gateway_state") == "running" \
                and not (fx.home / ".clover-update-heartbeat.json").exists():
            break
        time.sleep(1)
    pid = _read_state(fx.home).get("pid")
    second = subprocess.run(
        [str(fx.install / "venv" / "bin" / "python"), "-m", "clover_cli.main", "update", "--yes"],
        cwd=fx.install, env=fx.env, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=UPDATE_TIMEOUT)
    assert second.returncode == 0, (second.stdout + second.stderr)[-3000:]
    assert "nothing was changed" not in (second.stdout + second.stderr).lower()
    assert _read_state(fx.home).get("pid") == pid, "an up-to-date retry restarted the gateway"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        import psutil

        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return True

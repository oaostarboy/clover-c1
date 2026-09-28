"""Restart the gateway even when the updater dies.

WHY THIS EXISTS
---------------
``clover update`` stops a running gateway so it can swap the virtual
environment, then restarts it from an ``atexit`` handler. That handler runs on
a clean exit and on an unhandled exception. It does NOT run when the process
is killed by a signal, killed by the OS under memory pressure, or lost with
the machine.

Those are exactly the cases where the restart matters. Measured on this
codebase:

    updater finishes normally     restart runs
    updater crashes with an error restart runs
    updater killed (SIGTERM)      restart SKIPPED
    updater killed hard (SIGKILL) restart SKIPPED
    hard exit / power loss        restart SKIPPED

Reported from a Windows install on 2026-08-30: the updater stopped the
gateway, then vanished with no completion record. No gateway, no updater, no
supervisor. The assistant went silent and stayed silent until a human noticed.
It happened twice in one day, eleven hours apart, with byte-identical logs.

THE APPROACH
------------
Nothing inside the dying process can be trusted to clean up after it. So the
recovery lives in a SEPARATE process, started before the gateway is stopped.

The watcher polls a single file. While the updater lives, it refreshes that
file. If the file stops being refreshed and no gateway is running, the watcher
starts one and exits. If the updater finishes properly and restarts the
gateway itself, the watcher sees a healthy gateway and exits quietly.

The watcher is deliberately tiny and depends on nothing that an interrupted
update could have broken: no config parsing, no plugin loading, no network.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

# How often the watcher looks at the beacon.
POLL_SECONDS = 5.0

# How long the beacon may go unrefreshed before the watcher treats the updater
# as dead. Generous: a slow dependency sync can stall the updater's main thread
# for a while, and a false positive would start a second gateway.
BEACON_STALE_SECONDS = 90.0

# The watcher gives up after this long no matter what, so a forgotten watcher
# cannot linger for days.
WATCHER_MAX_LIFETIME_SECONDS = 3600.0

BEACON_NAME = ".clover-update-heartbeat.json"
ROLLBACK_MESSAGE = ("The update didn't start correctly, so I went back to the version "
                    "you had before. Nothing was lost. You can try again later.")


def _core_imports_healthy(root: Path, python: Path | None = None) -> bool:
    python = python or root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        return False
    try:
        return subprocess.run(
            [str(python), "-c", "import clover_cli.main, gateway.run"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _gateway_identity() -> tuple[int, float] | None:
    """Reuse the process-table gateway signature to detect a restart loop."""
    try:
        import psutil  # type: ignore
        for proc in psutil.process_iter(["cmdline", "create_time"]):
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "clover_cli.main" in cmd and " gateway" in f" {cmd}":
                return proc.pid, float(proc.info["create_time"])
    except Exception:
        pass
    return None


def _current_head(root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def probe_gateway(root: Path, *, timeout: float = 90, stable_seconds: float = 20,
                  poll: float = POLL_SECONDS, home: Path | None = None,
                  python: Path | None = None) -> bool:
    """One bounded startup probe: imports and continuously live gateway."""
    deadline = time.monotonic() + timeout
    stable_since = None
    stable_identity = None
    expected_sha = _current_head(root) if home else None
    while True:
        alive = _gateway_running()
        if alive and home and expected_sha:
            try:
                status = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
                if status.get("code_sha") and status["code_sha"] != expected_sha:
                    alive = False
            except (OSError, ValueError):
                pass  # Legacy gateways have no stamped status file.
        imports_ok = (_core_imports_healthy(root, python=python) if python is not None
                      else _core_imports_healthy(root)) if alive else False
        if alive and imports_ok:
            identity = _gateway_identity()
            if stable_since is None or (identity is not None and stable_identity != identity):
                stable_since = time.monotonic()
                stable_identity = identity
            if time.monotonic() - stable_since >= stable_seconds:
                return True
        else:
            stable_since = None
            stable_identity = None
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(max(poll, 0.01), max(0, deadline - time.monotonic())))


def _restart_from_beacon(data: dict[str, Any]) -> None:
    supervisor = data.get("supervisor")
    if supervisor in {"systemd", "launchd"}:
        root = Path(data["repo"])
        python = Path(data.get("venv_python") or
                      root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        if supervisor == "systemd":
            code = ("from clover_cli.update_cmd import _restart_systemd_gateway_units_best_effort as restart; "
                    "failed=[]; restart(failed); "
                    "assert not failed, f'Service restart failed: {failed}'")
        else:
            code = ("from clover_cli.update_cmd import _restart_macos_launchd_gateways as restart; "
                    "done=[]; failed=[]; restart(done, failed, 45.0); "
                    "assert not failed, f'Launchd restart failed: {failed}'")
        subprocess.run([str(python), "-c", code], cwd=root, check=True,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        return
    services = data.get("windows_services") or []
    if services and os.name == "nt":
        root = Path(data["repo"])
        # Standalone copy (no clover_constants import): same derivation as
        # _restart_from_beacon above; this branch only runs on Windows.
        python = Path(data.get("venv_python") or root / "venv" / "Scripts/python.exe")
        for name in services:
            subprocess.run(
                [str(python), "-c",
                 "import sys; from clover_cli.update_cmd import _restore_windows_gateway_service; "
                 "_restore_windows_gateway_service(sys.argv[1])", str(name)],
                cwd=root, check=True, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=70,
            )
        return
    argv = data.get("gateway_argv") or []
    if not argv:
        raise RuntimeError("No saved gateway restart command")
    kwargs: dict[str, Any] = {"cwd": data.get("cwd") or None, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def _rollback_checkout(data: dict[str, Any], beacon: Path) -> None:
    """Reset only a validated saved commit, repair the existing venv, restore config."""
    root = Path(data["repo"])
    sha = data["pre_pull_sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid saved pre-pull commit")
    if os.name == "nt":
        for service in data.get("windows_services") or []:
            subprocess.run(["sc", "stop", str(service)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=10)
            deadline = time.monotonic() + 60
            while True:
                state = subprocess.run(["sc", "query", str(service)], capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", timeout=10)
                if state.returncode == 0 and "STOPPED" in state.stdout:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Windows gateway service did not stop: {service}")
                time.sleep(0.25)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    parked = bool(dirty.stdout.strip())
    if parked:
        subprocess.run(["git", "stash", "push", "--include-untracked", "-m", "update-rollback-local-edits"],
                       cwd=root, check=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    subprocess.run(["git", "reset", "--hard", sha], cwd=root, check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if parked:
        reapplied = subprocess.run(["git", "stash", "apply", "stash@{0}"], cwd=root,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=60)
        if reapplied.returncode == 0:
            subprocess.run(["git", "stash", "drop", "stash@{0}"], cwd=root, check=True,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        # Conflicts stay in the stash; never discard the user's edits.
    snapshot_id = data.get("pre_update_snapshot_id")
    home = beacon.parent
    if snapshot_id and Path(snapshot_id).name == snapshot_id:
        config = home / "state-snapshots" / snapshot_id / "config.yaml"
        if config.is_file():
            shutil.copy2(config, home / "config.yaml")
    python = Path(data.get("venv_python") or
                  root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    if not python.is_file():
        venv_dir = python.parent.parent
        venv_arg = "venv" if venv_dir == root / "venv" else str(venv_dir)
        managed_uv = home / "bin" / ("uv.exe" if os.name == "nt" else "uv")
        uv = str(managed_uv) if managed_uv.is_file() else shutil.which("uv")
        if uv:
            create = [uv, "venv", venv_arg]
        else:
            bootstrap = next((exe for exe in (shutil.which("python3"), shutil.which("python"),
                                               str(sys.executable) if Path(sys.executable).is_file() else None)
                              if exe), None)
            if not bootstrap:
                raise RuntimeError("No Python interpreter available to recreate the managed venv")
            create = [bootstrap, "-m", "venv", venv_arg]
        subprocess.run(create, cwd=root, check=True, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    if not python.is_file():
        raise RuntimeError("Managed venv interpreter still missing after recreation")
    # Delegate to the same repair helper as `clover update` after the old
    # checkout is back in place. The watcher itself stays dependency-free.
    repair = ("from clover_cli.main import _install_python_dependencies_with_optional_fallback as install; "
              "import sys; install([sys.executable, '-m', 'pip'], group='all')")
    subprocess.run([str(python), "-c", repair], cwd=root, check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


def publish_gateway_verdict(beacon: Path, outcome: str) -> None:
    """Use existing /update IPC so even restored OLD gateway code can reply."""
    home = beacon.parent
    if outcome == "healthy" or not any(
        (home / name).exists() for name in (".update_pending.json", ".update_pending.claimed.json")
    ):
        return
    message = (ROLLBACK_MESSAGE if outcome == "rolled-back" else
               "The update failed and automatic recovery couldn't confirm the gateway is healthy. Check the update log.")
    (home / ".update_output.txt").write_text(message, encoding="utf-8")
    # Code LAST: the original gateway treats its presence as completion.
    (home / ".update_exit_code").write_text("1", encoding="utf-8")


def verify_or_rollback(data: dict[str, Any], beacon: Path, *, timeout: float = 90,
                       stable_seconds: float = 20) -> str:
    root = Path(data["repo"])
    python = Path(data["venv_python"]) if data.get("venv_python") else None
    if probe_gateway(root, timeout=timeout, stable_seconds=stable_seconds,
                     home=beacon.parent, python=python):
        return "healthy"
    log_dir = beacon.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "update-rollback.log"
    try:
        try:
            _rollback_checkout(data, beacon)
        except Exception:
            # The checkout may already be back at the saved commit even if
            # dependency repair failed. Try its gateway once; the second
            # health probe decides whether recovery actually succeeded.
            if _current_head(root) != data["pre_pull_sha"]:
                raise
            with log.open("a", encoding="utf-8") as handle:
                handle.write(traceback.format_exc())
        _restart_from_beacon(data)
        restored = probe_gateway(root, timeout=timeout, stable_seconds=stable_seconds,
                                 home=beacon.parent, python=python)
        outcome = "rolled-back" if restored else "rollback-unhealthy"
    except Exception as exc:
        logging.basicConfig(filename=str(log), level=logging.ERROR)
        logging.exception("Update rollback failed")
        with log.open("a", encoding="utf-8") as handle:
            handle.write(str(getattr(exc, "stdout", "") or "") + "\n")
            handle.write(str(getattr(exc, "stderr", "") or "") + "\n")
        outcome = "rollback-failed"
    receipt = log_dir / "update_receipts" / "latest.json"
    try:
        record = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else {}
        record.update(outcome=outcome, user_message=ROLLBACK_MESSAGE if outcome == "rolled-back" else
                      "The update failed and automatic recovery couldn't confirm the gateway is healthy. Check the update log.")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        logging.basicConfig(filename=str(log), level=logging.ERROR)
        logging.exception("Could not persist rollback result")
    print(ROLLBACK_MESSAGE if outcome == "rolled-back" else
          "The update failed and automatic recovery couldn't confirm the gateway is healthy. Check the update log.")
    publish_gateway_verdict(beacon, outcome)
    return outcome


def beacon_path(clover_home: Optional[Path] = None) -> Path:
    home = clover_home or Path(
        os.environ.get("CLOVER_HOME") or (Path.home() / ".clover")
    )
    return home / BEACON_NAME


def verification_pending(clover_home: Path) -> bool:
    """A finalized update receipt is provisional while restart probe runs."""
    try:
        data = json.loads(beacon_path(clover_home).read_text(encoding="utf-8"))
        return bool(data.get("pre_pull_sha") and data.get("repo"))
    except (OSError, ValueError):
        return False


def write_beacon(argv: list[str], *, clover_home: Optional[Path] = None,
                 pre_pull_sha: str | None = None, repo: str | None = None,
                 pre_update_snapshot_id: str | None = None,
                 windows_services: list[str] | None = None,
                 supervisor: str | None = None,
                 venv_python: str | None = None) -> Path:
    """Record how to restart the gateway, and that the updater is alive.

    ``argv`` is the command line of the gateway being stopped, captured before
    it is stopped. The watcher replays it verbatim.
    """
    path = beacon_path(clover_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updater_pid": os.getpid(),
        "refreshed_at": time.time(),
        "gateway_argv": list(argv),
        "cwd": os.getcwd(),
        "pre_pull_sha": pre_pull_sha,
        "repo": repo,
        "pre_update_snapshot_id": pre_update_snapshot_id,
        "windows_services": list(windows_services or []),
        "supervisor": supervisor,
        "venv_python": venv_python or (str(Path(sys.executable).absolute())
                                       if sys.prefix != sys.base_prefix else None),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


def refresh_beacon(*, clover_home: Optional[Path] = None) -> None:
    """Tell the watcher the updater is still working. Never raises."""
    path = beacon_path(clover_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["refreshed_at"] = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def mark_ready_for_probe(*, clover_home: Optional[Path] = None) -> bool:
    """CLI update is done; allow the independent watcher to probe before exit."""
    path = beacon_path(clover_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not (data.get("pre_pull_sha") and data.get("repo")):
            return False
        data["ready_for_probe"] = True
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
        return True
    except (OSError, ValueError):
        return False


def wait_for_cli_verdict(*, clover_home: Optional[Path] = None,
                         timeout: float = 600) -> bool:
    """Print the final rollback result on the same CLI that ran update."""
    if not mark_ready_for_probe(clover_home=clover_home):
        return True  # No post-pull watcher was armed on this path.
    beacon = beacon_path(clover_home)
    deadline = time.monotonic() + timeout
    while beacon.exists() and time.monotonic() < deadline:
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    if beacon.exists():
        print("The update could not be verified. Check ~/.clover/logs/update-rollback.log.")
        return False
    try:
        receipt = beacon.parent / "logs" / "update_receipts" / "latest.json"
        record = json.loads(receipt.read_text(encoding="utf-8"))
        outcome = record.get("outcome")
        if outcome in {"rolled-back", "rollback-unhealthy", "rollback-failed"}:
            print(record.get("user_message") or "The update failed; check ~/.clover/logs/.")
            return False
    except (OSError, ValueError):
        print("The update outcome could not be verified. Check ~/.clover/logs/.")
        return False
    return True


def clear_beacon(*, clover_home: Optional[Path] = None) -> None:
    """The update finished. Stand the watcher down. Never raises."""
    try:
        path = beacon_path(clover_home)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("pre_pull_sha") and data.get("repo"):
            # The separate watcher must survive normal updater exit as well:
            # the freshly restarted gateway can still crash after atexit.
            return
        path.unlink()
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, encoding="utf-8", timeout=10,
            ).stdout
            return str(pid) in out
        except Exception:
            return True  # unknown means "assume alive": never restart on a guess
    try:
        os.kill(pid, 0)  # windows-footgun: ok (POSIX only; nt returns above)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True
    return True


def _gateway_running() -> bool:
    """Is a gateway process alive right now?

    Read from the process table rather than a status file: a status file is
    exactly the thing that goes stale when a process dies badly.
    """
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None  # type: ignore

    if psutil is not None:
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
            except Exception:
                continue
            if "clover_cli.main" in cmd and " gateway" in f" {cmd}":
                return True
        return False

    # No psutil: fall back to the platform's own process listing.
    try:
        if os.name == "nt":  # pragma: no cover
            out = subprocess.run(
                ["wmic", "process", "get", "commandline"],
                capture_output=True, text=True, encoding="utf-8", timeout=20,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-eo", "args"], capture_output=True, text=True, encoding="utf-8", timeout=20
            ).stdout
    except Exception:
        return True  # cannot tell: assume healthy rather than start a second one
    for line in out.splitlines():
        if "clover_cli.main" in line and " gateway" in line:
            return True
    return False


def watch(beacon: Path, *, poll: float = POLL_SECONDS) -> str:
    """Watch one beacon. Returns what happened, for the log and the tests.

    Outcomes:
      "update-finished"  the updater cleared the beacon; nothing to do
      "gateway-healthy"  the updater died, but a gateway is running anyway
      "restarted"        the updater died and the watcher started the gateway
      "no-argv"          the updater died and left no restart command
      "expired"          nothing resolved within the lifetime ceiling
    """
    started = time.monotonic()
    while True:
        if time.monotonic() - started > WATCHER_MAX_LIFETIME_SECONDS:
            return "expired"

        try:
            data: dict[str, Any] = json.loads(beacon.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "update-finished"
        except Exception:
            time.sleep(poll)
            continue

        updater_pid = int(data.get("updater_pid") or 0)
        refreshed_at = float(data.get("refreshed_at") or 0.0)
        age = time.time() - refreshed_at

        updater_gone = not _pid_alive(updater_pid)
        beacon_stale = age > BEACON_STALE_SECONDS

        if updater_gone or data.get("ready_for_probe") or (beacon_stale and not data.get("pre_pull_sha")):
            if data.get("pre_pull_sha") and data.get("repo"):
                try:
                    head = subprocess.run(
                        ["git", "rev-parse", "HEAD"], cwd=data["repo"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                    )
                    if head.returncode == 0 and head.stdout.strip() == data["pre_pull_sha"]:
                        if not _gateway_running() and (data.get("gateway_argv") or data.get("windows_services") or data.get("supervisor")):
                            _restart_from_beacon(data)
                        beacon.unlink(missing_ok=True)
                        return "update-finished"
                    result = verify_or_rollback(data, beacon)
                    beacon.unlink(missing_ok=True)
                    return result
                except Exception:
                    logs = beacon.parent / "logs"
                    logs.mkdir(parents=True, exist_ok=True)
                    with (logs / "update-rollback.log").open("a", encoding="utf-8") as handle:
                        handle.write(traceback.format_exc())
                    receipt = logs / "update_receipts" / "latest.json"
                    receipt.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        record = json.loads(receipt.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        record = {}
                    record.update(outcome="rollback-failed", user_message=(
                        "The update failed and automatic recovery couldn't confirm the gateway is healthy. Check the update log."))
                    receipt.write_text(json.dumps(record), encoding="utf-8")
                    publish_gateway_verdict(beacon, "rollback-failed")
                    beacon.unlink(missing_ok=True)
                    return "rollback-failed"
            # Give the updater's own atexit restart a moment to win the race.
            time.sleep(poll)
            if _gateway_running():
                return "gateway-healthy"

            argv = list(data.get("gateway_argv") or [])
            if not argv:
                return "no-argv"

            cwd = data.get("cwd") or None
            kwargs: dict[str, Any] = {"cwd": cwd, "close_fds": True}
            if os.name == "nt":  # pragma: no cover
                kwargs["creationflags"] = (
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                )
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(argv, **kwargs)
            try:
                beacon.unlink()
            except Exception:
                pass
            return "restarted"

        time.sleep(poll)


def main(argv: list[str]) -> int:  # pragma: no cover - process entry point
    if len(argv) < 2:
        return 2
    print(watch(Path(argv[1])))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))

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
import os
import subprocess
import sys
import time
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


def beacon_path(clover_home: Optional[Path] = None) -> Path:
    home = clover_home or Path(
        os.environ.get("CLOVER_HOME") or (Path.home() / ".clover")
    )
    return home / BEACON_NAME


def write_beacon(argv: list[str], *, clover_home: Optional[Path] = None) -> Path:
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


def clear_beacon(*, clover_home: Optional[Path] = None) -> None:
    """The update finished. Stand the watcher down. Never raises."""
    try:
        beacon_path(clover_home).unlink()
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        except Exception:
            return True  # unknown means "assume alive": never restart on a guess
    try:
        os.kill(pid, 0)
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
                capture_output=True, text=True, timeout=20,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-eo", "args"], capture_output=True, text=True, timeout=20
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

        if updater_gone or beacon_stale:
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

"""Fixed test-only process: argv tails are data, NEVER commands or code.

The shared pytest guard pins this file's bytes. Keep modes and tails finite;
no plugins, gateway entry point, user scripts, or caller-supplied paths.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ("-m", "clover_cli.main", "gateway", "run")
TAILS = {
    (), ("totally", "unrelated"), ("-m", "clover_cli.main"), GATEWAY,
    ("-m", "clover_cli.main", "gateway", "status"),
    ("-m", "clover_cli.main", "serve"),
    ("-m", "clover_cli.main", "serve", "--port", "8127"),
    ("-m", "clover_cli.main", "dashboard"),
    ("-m", "clover_cli.main", "kanban", "--preserve-cache"),
    *(('C:\\Users\\' + letter * 90 + '\\.clover-runtime', *GATEWAY)
      for letter in ("x", "y")),
}


def _bootstrap_read_only_cli() -> None:
    # -I does not suppress application import side effects. Install these
    # process-local TEST-ONLY fences before importing scanner dependencies.
    # Keep sys.argv intact: classifiers must see the real process command line.
    os.environ["CLOVER_IMPORT_PROBE"] = "1"  # existing early-recovery opt-out
    from clover_cli import _install_repair

    def refuse_launcher_repair(*_args, **_kwargs):
        raise RuntimeError("inert holder refuses startup launcher mutation")

    _install_repair.ensure_windows_bin_launchers = refuse_launcher_repair

    from clover_cli import env_loader

    load_dotenv = env_loader.load_clover_dotenv

    def load_without_external_secrets(**kwargs):
        # Use the existing explicit opt-out even when main.py requests True.
        # Leave the synthetic hostile config in place; do not replace it.
        kwargs["load_external_secrets"] = False
        return load_dotenv(**kwargs)

    env_loader.load_clover_dotenv = load_without_external_secrets


def scan() -> int:
    # -I makes imports independent of caller cwd/environment. Only the reviewed
    # repository is added, to exercise the REAL read-only detector/classifiers.
    sys.path.insert(0, str(ROOT))
    _bootstrap_read_only_cli()
    import psutil
    from clover_cli.update_cmd import (
        _classify_concurrent_instance, _detect_venv_python_processes,
    )
    from gateway.status import looks_like_gateway_command_line

    ancestors = []
    gateway_ancestors = []
    for ancestor in psutil.Process().parents():
        argv = " ".join(ancestor.cmdline() or [])
        ancestors.append({"pid": ancestor.pid,
                          "classification": _classify_concurrent_instance(ancestor.pid)})
        if looks_like_gateway_command_line(argv):
            gateway_ancestors.append(ancestor.pid)
    matches = _detect_venv_python_processes()
    print(json.dumps({"pid": os.getpid(), "gateway_ancestors": gateway_ancestors,
                      "ancestors": ancestors, "pids": [p for p, _, _ in matches]}))
    return 0


def parent_scan() -> int:
    # This is the ONLY child spawn: a fixed same-file mode, no test code/path.
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "scan"],
        cwd=str(ROOT), env=dict(os.environ), shell=False, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = child.communicate(timeout=12)
        sys.stdout.write(out)
        sys.stderr.write(err)
        return child.returncode
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    mode, tail = sys.argv[1], tuple(sys.argv[2:])
    if mode == "sleep" and tail in TAILS:
        time.sleep(15)  # finite lifetime even if test setup/cleanup fails
        return 0
    if mode == "parent-scan" and tail == GATEWAY:
        return parent_scan()
    if mode == "scan" and not tail:
        return scan()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

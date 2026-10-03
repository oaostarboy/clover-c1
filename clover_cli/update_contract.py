"""Image-managed install refusal contract (#91277 Phase 3).

One shared admission gate for every surface that can start an in-place
``clover update`` mutation (CLI apply, CLI --check, dashboard update
endpoint). The decision layers:

1. **Baked provenance marker** (``/etc/clover/image-provenance.json``,
   written by the image build — see :mod:`clover_cli.image_provenance`):
   authoritative ground truth that this filesystem came from an immutable
   image. Fail-closed: a present-but-malformed marker still refuses.
2. **Filesystem heuristics** (``detect_install_method()``): the pre-existing
   docker/nix/apt detection, kept as the fallback for images built before
   the marker existed and for package-managed installs that have no image
   marker at all.

A refusal prints the real update command for the deployment kind, records a
``refused`` receipt (so fleet tooling sees "this install cannot self-update,
use <command>" instead of a silent non-update), and exits 2 on CLI surfaces.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdateRefusal:
    """Why an in-place update is refused, and what to run instead."""

    code: str              # image-marker | image-marker-invalid | docker | nix | apt
    message: str           # full user-facing text (multi-line ok)
    update_command: str    # the one-line remediation command


def evaluate_update_admission(project_root: Path) -> Optional[UpdateRefusal]:
    """Return an :class:`UpdateRefusal` when in-place update must not run.

    ``None`` means the install is eligible for in-place update (git checkout
    or unknown-but-mutable). Never raises; on any internal error it falls
    back to the heuristic layer only.
    """
    # Layer 1: baked provenance marker — authoritative when present.
    try:
        from clover_cli.image_provenance import read_image_provenance

        provenance = read_image_provenance()
        if provenance is not None:
            from clover_cli.config import (
                format_docker_update_message,
                recommended_update_command_for_method,
            )

            if not provenance.valid:
                # Present but malformed: still image-managed — an integrity
                # defect is never permission to mutate the image in place.
                command = recommended_update_command_for_method("docker")
                return UpdateRefusal(
                    code="image-marker-invalid",
                    message=(
                        "✗ This install is image-managed, but its provenance "
                        f"marker is invalid ({provenance.error}).\n"
                        "  In-place update is disabled. Update by pulling a "
                        f"new image:\n    {command}"
                    ),
                    update_command=command,
                )
            manager = provenance.manager
            if manager == "docker":
                return UpdateRefusal(
                    code="image-marker",
                    message=format_docker_update_message(),
                    update_command=recommended_update_command_for_method("docker"),
                )
            command = recommended_update_command_for_method(manager)
            return UpdateRefusal(
                code="image-marker",
                message=command,
                update_command=command,
            )
    except Exception as exc:
        logger.debug("Image provenance check failed (using heuristics): %s", exc)

    # Layer 2: pre-existing filesystem heuristics, verbatim semantics.
    try:
        from clover_cli.config import (
            detect_install_method,
            format_docker_update_message,
            is_nix_install_method,
            recommended_update_command_for_method,
        )

        method = detect_install_method(project_root)
        if method == "docker":
            return UpdateRefusal(
                code="docker",
                message=format_docker_update_message(),
                update_command=recommended_update_command_for_method("docker"),
            )
        if is_nix_install_method(method) or method == "apt":
            command = recommended_update_command_for_method(method)
            return UpdateRefusal(
                code=method if method == "apt" else "nix",
                message=command,
                update_command=command,
            )
    except Exception as exc:
        logger.debug("Install-method admission check failed: %s", exc)
    return None


def record_refusal_receipt(refusal: UpdateRefusal) -> None:
    """Write a minimal ``refused`` receipt for a blocked update attempt.

    Gives fleet tooling a durable record that an update was ATTEMPTED and
    refused ("not updatable in place, use <command>") instead of a silent
    nothing. Best-effort; never raises.
    """
    try:
        from clover_cli.update_receipt import (
            begin_update_receipt,
            finalize_update_receipt,
            record_step,
        )

        begin_update_receipt()
        record_step(
            "admission",
            False,
            f"not updatable in place ({refusal.code}); use: {refusal.update_command}",
        )
        finalize_update_receipt("refused", stop_reason=refusal.code)
    except Exception as exc:
        logger.debug("Could not record refusal receipt: %s", exc)


# Exit code the updater uses to say "I declined to proceed", as distinct from
# "I tried and broke". Emitted when a live process still holds the venv that
# the update must replace: the updater resumes what it paused and changes
# nothing (receipt outcome "refused", pre_update.sha == post_update.sha).
#
# This is a SAFE outcome. Anything rendering it to a user must not call it a
# failure -- doing so sends people to hunt a breakage that never happened.
UPDATE_EXIT_REFUSED = 2

UPDATE_REFUSED_HEADLINE = "\u26a0\ufe0f Update skipped \u2014 nothing was changed."

def update_refused_detail(refusal: dict | None) -> str:
    """The refusal detail for chat: the ACTUAL holders when the updater recorded them."""
    holders = (refusal or {}).get("holders") if isinstance(refusal, dict) else None
    if not holders:
        return UPDATE_REFUSED_DETAIL
    lines = ["These processes still hold the Python environment the update needs to replace:"]
    for h in holders:
        try:
            lines.append(f"• PID {int(h.get('pid'))} {h.get('name', '')}: {str(h.get('cmdline', ''))[:160]}")
        except Exception:
            continue
    more = int((refusal or {}).get("more") or 0)
    if more:
        lines.append(f"• …and {more} more")
    lines.append("")
    lines.append("Nothing was changed. Stop them (or run `clover update` from a terminal) and try again.")
    return "\n".join(lines)


UPDATE_REFUSED_DETAIL = (
    "The running gateway still holds the Python environment the update needs "
    "to replace, so the updater declined rather than force-stopping it. Your "
    "install is untouched and still running the previous version.\n\n"
    "To apply it, run `clover update` from a terminal outside the gateway."
)


#: Environment that only means something inside one update's clover.exe
#: hand-off. A gateway relaunched by the hand-off child inherits it; left in
#: place, the gateway's next chat /update believed it WAS that hand-off
#: child, skipped the pull and only "finished the dependency install"
#: (Windows runner, 2026-09-29).
UPDATE_HANDOFF_ONLY_ENV = ("CLOVER_UPDATE_REEXEC",)


def drop_update_handoff_env(env: Optional[dict] = None) -> dict:
    """Remove hand-off-only variables from *env* (``os.environ`` by default).

    Mutates and returns *env*, so a gateway can clean its own environment at
    start and a spawn site can clean a copy.
    """
    target = os.environ if env is None else env
    for key in UPDATE_HANDOFF_ONLY_ENV:
        target.pop(key, None)
    return target


# ---------------------------------------------------------------------------
# Escaping the gateway's systemd cgroup (Linux, user units)
# ---------------------------------------------------------------------------

# A child of the gateway stays in the gateway unit's cgroup even after
# ``setsid``; ``KillMode=mixed`` then SIGKILLs it when the unit restarts.  A
# transient user scope moves the child into its own ``run-*.scope``.
_SYSTEMD_RUN_SCOPE_FLAGS = ("--user", "--scope", "--quiet", "--collect", "--")
_SCOPE_PROBE_TIMEOUT_SECONDS = 10

_scope_probe_lock = threading.Lock()
# None = not probed yet; otherwise the cached result for this process.
_scope_probe_result: Optional[bool] = None


def systemd_user_scope_argv(
    command: list[str], *, systemd_run: Optional[str] = None
) -> Optional[list[str]]:
    """Return *command* prefixed to run in a transient user scope.

    ``None`` when ``systemd-run`` is not installed, so each caller decides
    whether to fail closed or fall back.

    ``systemd-run`` runs systemd's own ``$`` expansion over its arguments
    (``$$`` becomes ``$``, ``${VAR}`` and a whole-word ``$VAR``/``$?`` are
    substituted, unset ones with an empty string).  Every ``$`` is doubled so
    the program receives each argument exactly as written; this matters to a
    ``bash -c`` command that uses ``$?`` or ``$$``.
    """
    systemd_run = systemd_run or shutil.which("systemd-run")
    if not systemd_run:
        return None
    return [
        systemd_run,
        *_SYSTEMD_RUN_SCOPE_FLAGS,
        *(arg.replace("$", "$$") for arg in command),
    ]


def _own_cgroup_path() -> Optional[str]:
    """This process's unified-hierarchy cgroup path, or ``None``."""
    try:
        with open("/proc/self/cgroup", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                _hier, sep, path = line.strip().partition("::")
                if sep:
                    return path
    except OSError:
        pass
    return None


def running_in_user_systemd_unit() -> bool:
    """True when this process lives in a *user* systemd service's cgroup.

    ``/user.slice/.../<name>.service`` is a user unit; ``/system.slice/...``
    is a system unit (where ``systemd-run --user`` has no session bus), and a
    ``session-N.scope`` is a login shell with no unit to be killed with.
    """
    if not sys.platform.startswith("linux"):
        return False
    path = _own_cgroup_path()
    if not path or "/user.slice/" not in path:
        return False
    return any(part.endswith(".service") for part in path.split("/"))


def _with_user_bus_env(env: dict) -> dict:
    """Copy of *env* that ``systemd-run --user`` can reach the user bus with."""
    out = dict(env)
    uid = os.getuid()  # windows-footgun: ok — Linux-only helper, guarded by sys.platform
    runtime = out.get("XDG_RUNTIME_DIR")

    def ours(path: Optional[str]) -> bool:
        return bool(path) and os.path.isdir(path) and os.stat(path).st_uid == uid

    try:
        if not ours(runtime) and ours(f"/run/user/{uid}"):
            runtime = out["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        if runtime and "DBUS_SESSION_BUS_ADDRESS" not in out:
            bus = os.path.join(runtime, "bus")
            if os.path.exists(bus):
                out["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    except OSError:
        pass
    return out


def _user_scope_usable(systemd_run: str, env: dict) -> bool:
    """Whether ``systemd-run --user --scope`` can create a scope here (cached).

    ``--scope`` execs the command, so a scope that cannot be created is only
    visible as a fast non-zero exit of the real launch.  Probing with ``true``
    first lets the caller fall back without ever launching the updater twice.
    """
    global _scope_probe_result
    with _scope_probe_lock:
        if _scope_probe_result is not None:
            return _scope_probe_result
        argv = systemd_user_scope_argv(["true"], systemd_run=systemd_run)
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=_SCOPE_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
            ok = proc.returncode == 0
            if not ok:
                logger.warning(
                    "systemd-run --user --scope probe exited %s", proc.returncode
                )
        except (OSError, subprocess.SubprocessError) as exc:
            ok = False
            logger.warning("systemd-run --user --scope probe failed: %s", exc)
        _scope_probe_result = ok
        return ok


def escape_gateway_cgroup(argv: list[str], env: dict) -> tuple[list[str], dict]:
    """Wrap a gateway-spawned updater so a gateway unit restart cannot kill it.

    Only Linux under a user systemd unit is touched; every other platform and
    system-scope units get ``(argv, env)`` back unchanged.  If ``systemd-run``
    is missing or cannot create a scope, a warning is logged and the caller
    keeps its plain ``setsid`` spawn.
    """
    if not running_in_user_systemd_unit():
        return argv, env
    systemd_run = shutil.which("systemd-run")
    if not systemd_run:
        logger.warning(
            "systemd-run not found; the chat-launched updater stays inside the "
            "gateway cgroup and may be killed when the gateway unit restarts"
        )
        return argv, env
    scope_env = _with_user_bus_env(env)
    if not _user_scope_usable(systemd_run, scope_env):
        logger.warning(
            "systemd-run --user --scope is unavailable; the chat-launched updater "
            "stays inside the gateway cgroup and may be killed when the gateway "
            "unit restarts"
        )
        return argv, env
    return systemd_user_scope_argv(argv, systemd_run=systemd_run), scope_env

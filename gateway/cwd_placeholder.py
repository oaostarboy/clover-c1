"""Resolve gateway ``terminal.cwd`` placeholder values to ``TERMINAL_CWD``.

When ``terminal.cwd`` is unset or a placeholder (``.``, ``auto``, ``cwd``),
the gateway must not blindly map host ``Path.home()`` into container backends.
Docker with workspace mounting still needs an explicit host path signal
(``MESSAGING_CWD`` or an absolute config path) for ``terminal_tool`` to map
``/host/project`` → ``/workspace``.
"""

from __future__ import annotations

import os
from typing import Mapping

CWD_PLACEHOLDERS = frozenset({".", "auto", "cwd"})


def _truthy_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes"}


def resolve_placeholder_terminal_cwd(
    *,
    configured_cwd: str,
    terminal_backend: str,
    messaging_cwd: str | None,
    docker_mount_cwd_to_workspace: bool,
    home_fallback: str,
) -> str | None:
    """Return the ``TERMINAL_CWD`` value to set, or ``None`` to leave it unset.

    Cases:
      - **local** + placeholder → ``MESSAGING_CWD`` or ``home_fallback``
      - **docker** + placeholder + mount on + host ``MESSAGING_CWD`` → host path
        (for ``terminal_tool`` ``/workspace`` mapping)
      - **docker** + placeholder + mount off → ``None`` (sandbox default)
      - other non-local backends + placeholder → ``None``
    """
    if configured_cwd and configured_cwd not in CWD_PLACEHOLDERS:
        return configured_cwd

    backend = (terminal_backend or "local").strip().lower()
    if backend == "local":
        messaging = (messaging_cwd or "").strip()
        return messaging or home_fallback

    if backend == "docker" and docker_mount_cwd_to_workspace:
        messaging = (messaging_cwd or "").strip()
        if messaging and messaging not in CWD_PLACEHOLDERS:
            return messaging

    return None


def startup_terminal_cwd(environ: Mapping[str, str], process_cwd: str) -> str | None:
    """Return the folder a local terminal's first command starts in, or ``None``.

    Mirrors ``tools.terminal_tool._get_env_config``'s local default
    (``TERMINAL_CWD``, else the process cwd, with ``~`` expanded) as it stands
    AFTER the gateway's placeholder normalization. Pure: the gateway calls it
    once at startup so later ``TERMINAL_CWD`` writes (workdir cron jobs mutate
    the process-global) cannot move the answer.

    Only an absolute folder on the local backend is a usable host start
    folder; anything else (container/ssh backends, a relative or placeholder
    value that survived normalization) returns ``None`` so callers keep
    refusing rather than guess.
    """
    backend = (environ.get("TERMINAL_ENV") or "local").strip().lower()
    if backend != "local":
        return None
    candidate = (environ.get("TERMINAL_CWD") or "").strip() or process_cwd
    if not candidate:
        return None
    expanded = os.path.expanduser(candidate)
    return expanded if os.path.isabs(expanded) else None

"""Resolve CLOVER_HOME for standalone skill scripts.

Skill scripts may run outside the Clover process (e.g. system Python,
nix env, CI) where ``clover_constants`` is not importable.  This module
provides the same ``get_clover_home()`` and ``display_clover_home()``
contracts as ``clover_constants`` without requiring it on ``sys.path``.

When ``clover_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``clover_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``CLOVER_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from clover_constants import display_clover_home as display_clover_home
    from clover_constants import get_clover_home as get_clover_home
except (ModuleNotFoundError, ImportError):

    def get_clover_home() -> Path:
        """Return the Clover home directory (default: ~/.clover).

        Mirrors ``clover_constants.get_clover_home()``."""
        val = os.environ.get("CLOVER_HOME", "").strip()
        return Path(val) if val else Path.home() / ".clover"

    def display_clover_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``clover_constants.display_clover_home()``."""
        home = get_clover_home()
        try:
            return "~/" + home.relative_to(Path.home()).as_posix()
        except ValueError:
            return str(home)

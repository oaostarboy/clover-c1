"""Resolve CLOVER_HOME for standalone skill scripts.

Skill scripts may run outside the Clover process (system Python, nix env,
CI) where ``clover_constants`` is not importable.  This module provides the
same ``get_clover_home()`` contract without requiring it on ``sys.path``.

When ``clover_constants`` IS available it is used directly so profile
resolution and any future enhancements are picked up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from clover_constants import get_clover_home as get_clover_home
except (ModuleNotFoundError, ImportError):

    def get_clover_home() -> Path:
        """Return the Clover home directory (default: ``~/.clover``)."""
        val = os.environ.get("CLOVER_HOME", "").strip()
        return Path(val) if val else Path.home() / ".clover"

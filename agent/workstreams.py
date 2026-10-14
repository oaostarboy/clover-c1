"""Opt-in workstream ledger policy (PLAN-v1): scope, settings, receipts,
feedback, novelty, evidence and bounded context selection.

Storage lives in ``cron/notepad.py`` (the existing profile-local SQLite owner).
This module is pure policy plus a thin facade: every API is empty/no-op when
``proactive.enabled`` is false, and nothing here grants authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from clover_constants import get_clover_home


def current_profile() -> str:
    return str(get_clover_home().resolve())


@dataclass(frozen=True)
class Scope:
    """Isolation key. A row is visible only when all four fields match."""

    profile: str
    project: str
    audience: str
    root: str

    @property
    def private(self) -> bool:
        return self.audience == "private" or self.audience.startswith("private:")


def make_scope(
    project: str, *, audience: str = "private", root: str = "",
    profile: Optional[str] = None,
) -> Scope:
    if not str(project or "").strip():
        raise ValueError("project must be non-empty")
    if not str(audience or "").strip():
        raise ValueError("audience must be non-empty")
    return Scope(
        profile=str(profile) if profile else current_profile(),
        project=str(project).strip(),
        audience=str(audience).strip(),
        root=str(root or ""),
    )

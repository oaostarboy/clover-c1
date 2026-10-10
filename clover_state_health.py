"""Profile-level session-storage health: one process-wide latch per state.db path.

Adapted from NousResearch/hermes-agent bd945ec384 / f925b01791 (MIT), trimmed to
the latch itself.

A structurally corrupt state.db used to look different on every surface: the
session list returned an empty page or a 500, readiness stayed green because its
schema read still worked, and writes failed one by one with a generic error.
None of them said "the history store is damaged".  This module is the single
place that fact is recorded.  ``SessionDB`` write/read helpers and the readiness
probe publish into it; ``gateway.readiness`` and the session-list endpoints read
from it, so they cannot disagree.

Only *structural* corruption latches: a bare ``SQLITE_CORRUPT`` /
``SQLITE_NOTADB`` with no FTS provenance.  FTS-scoped damage has its own
rebuild / fail-open path with canonical rows intact, and a malformed-schema row
is healed by the web open path, so neither is reported here.

The latch never clears on its own.  A corrupt image does not heal, and a store
that flickers between "ok" and "corrupt" is the silent failure this replaces.
It resets when the process restarts (the recovery boundary: stop Clover, repair
or restore, start again).  Nothing is written to disk — the file a marker would
describe is the one that is damaged.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STORAGE_OK = "ok"
STORAGE_CORRUPT = "corrupt"

# Machine-readable payload for HTTP 503 responses (``detail``).
CORRUPT_STORE_DETAIL = {
    "error": "state_db_corrupt",
    "message": (
        "state.db is damaged — your history store needs repair. "
        "Stop Clover, run `clover doctor`, or restore a snapshot."
    ),
}

_lock = threading.Lock()
_corrupt: dict[str, str] = {}  # resolved db path -> first error text (log only, never served)

# SQLITE_CORRUPT_VTAB: SQLite itself attributes the damage to a virtual table (FTS).
_SQLITE_CORRUPT_VTAB = getattr(sqlite3, "SQLITE_CORRUPT_VTAB", 267)
_FTS_OBJECT_RE = re.compile(r"messages_fts|\bfts5\b")


def _key(db_path) -> str:
    return str(Path(db_path).expanduser().resolve(strict=False))


def is_fts_scoped_corruption_error(exc) -> bool:
    """Corruption SQLite attributes to the FTS index layer.

    A known result code outranks prose: ``SQLITE_CORRUPT_VTAB`` is FTS-scoped even
    with the generic malformed-image text older builds emit, while bare
    ``SQLITE_CORRUPT`` / ``SQLITE_NOTADB`` carry no object scope and fail closed.
    Without a code (Python < 3.11, RPC-wrapped strings) only an ``fts5:`` corruption
    report or a corruption marker naming a ``messages_fts*`` object counts.
    """
    if exc is None:
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code == _SQLITE_CORRUPT_VTAB
    text = (exc if isinstance(exc, str) else str(exc)).lower()
    return bool(_FTS_OBJECT_RE.search(text))


def is_structural_corruption_error(exc: BaseException) -> bool:
    """Canonical B-tree/schema/freelist damage: a corrupt/NOTADB error SQLite does not
    scope to the FTS index, and not the malformed-schema case the web open path repairs."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    # Lazy: clover_state imports this module.
    from clover_state import classify_persistence_error, is_malformed_schema_error

    return (
        not is_fts_scoped_corruption_error(exc)
        and not is_malformed_schema_error(exc)
        and classify_persistence_error(exc) == "corrupt"
    )


def mark_storage_corrupt(db_path, reason: object) -> None:
    """Latch *db_path* as corrupt for the life of this process (idempotent)."""
    key = _key(db_path)
    with _lock:
        if key in _corrupt:
            return
        _corrupt[key] = str(reason)
    logger.error(
        "state.db at %s is structurally corrupt (%s); session storage is reported as "
        "corrupt until Clover restarts on a recovered or restored file. Stop Clover, "
        "then run `clover doctor` or restore a snapshot.",
        db_path, reason,
    )


def note_storage_error(db_path, exc: BaseException) -> bool:
    """Latch *db_path* when *exc* is structural corruption; True when it was."""
    if not is_structural_corruption_error(exc):
        return False
    mark_storage_corrupt(db_path, exc)
    return True


def storage_state(db_path) -> str:
    """``"corrupt"`` once this process has seen structural corruption on *db_path*."""
    with _lock:
        return STORAGE_CORRUPT if _key(db_path) in _corrupt else STORAGE_OK


def storage_corrupt_reason(db_path) -> Optional[str]:
    """The first error text latched for *db_path* (logs/diagnostics only), or None."""
    with _lock:
        return _corrupt.get(_key(db_path))


def reset_storage_state(db_path=None) -> None:
    """Forget the latch for *db_path* (all paths when None). For tests and a verified
    in-process recovery; nothing in the runtime clears it on its own."""
    with _lock:
        if db_path is None:
            _corrupt.clear()
        else:
            _corrupt.pop(_key(db_path), None)


__all__ = [
    "CORRUPT_STORE_DETAIL",
    "STORAGE_CORRUPT",
    "STORAGE_OK",
    "is_fts_scoped_corruption_error",
    "is_structural_corruption_error",
    "mark_storage_corrupt",
    "note_storage_error",
    "reset_storage_state",
    "storage_corrupt_reason",
    "storage_state",
]

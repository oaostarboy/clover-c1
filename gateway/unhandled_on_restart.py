"""Queued human messages a restart did not handle.

Human messages parked behind a running turn live only in memory.  An orderly
shutdown appends one JSON-lines row per such message to
``<profile home>/gateway_unhandled_on_restart.jsonl``; the next startup tells
each chat once that the message was not handled so the person can resend it.
Nothing is replayed, so no session or routing decision is made here.

Rows carry no session ids.  Every read, append, claim and rewrite runs under a
cross-process file lock (a gateway still stopping, a fresh one and a standalone
one may share a profile home).  A notice is sent only for rows the sender
*claimed* — removed from the file under the lock — before sending, so two
processes never both send, and a crash or a failed send can never produce a
second notice.  A row whose adapter is not connected waits for the next startup
and is dropped after ``MAX_STARTUPS`` startups.  A line that cannot be read is
moved to ``<file>.corrupt`` and logged, never silently dropped.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

FILE_NAME = "gateway_unhandled_on_restart.jsonl"
MAX_STARTUPS = 3
PREVIEW_CHARS = 120
NOTICE_PREVIEWS = 3

LOCK_TIMEOUT_SECONDS = 30.0
_lock_holder = threading.local()


def file_path(home: Path) -> Path:
    return Path(home) / FILE_NAME


def _media_marker(event: Any, index: int) -> str:
    types = getattr(event, "media_types", None) or []
    media_type = str(types[index]) if index < len(types) and types[index] else ""
    if media_type:
        return "[photo]" if media_type.startswith("image/") else "[file]"
    message_type = getattr(getattr(event, "message_type", None), "value", "")
    return "[photo]" if message_type == "photo" else "[file]"


def preview_for(event: Any) -> str:
    """First 120 chars of the message, newlines collapsed, media as markers."""
    text = " ".join(str(getattr(event, "text", "") or "").split())
    markers = " ".join(
        _media_marker(event, i) for i in range(len(getattr(event, "media_urls", None) or []))
    )
    if not markers:
        return text[:PREVIEW_CHARS]
    if not text:
        return markers[:PREVIEW_CHARS]
    room = PREVIEW_CHARS - len(markers) - 1
    return f"{text[:max(room, 0)]} {markers}".strip()[:PREVIEW_CHARS]


def row_for(event: Any, profile: str) -> Optional[dict]:
    """The row for one queued human event, or ``None`` when it has no content."""
    preview = preview_for(event)
    source = getattr(event, "source", None)
    if not preview or source is None or not getattr(source, "chat_id", None):
        return None
    platform = getattr(source, "platform", None)
    stamp = getattr(event, "timestamp", None)
    try:
        queued_at = stamp.timestamp()
    except Exception:
        queued_at = time.time()
    return {
        "id": uuid.uuid4().hex,
        "platform": getattr(platform, "value", None) or str(platform),
        "profile": profile,
        "chat_id": str(source.chat_id),
        "chat_type": getattr(source, "chat_type", None),
        "thread_id": str(source.thread_id) if getattr(source, "thread_id", None) else None,
        "user_id": getattr(source, "user_id", None),
        "user_name": getattr(source, "user_name", None) or getattr(event, "user_name", None),
        "scope_id": getattr(source, "scope_id", None),
        "delivered_via_upstream_relay": getattr(source, "delivered_via_upstream_relay", False) is True,
        "preview": preview,
        "queued_at": queued_at,
        "attempts": 0,
    }


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def _locked(path: Path):
    """Exclusive cross-process lock for one notice file (the repo's file-lock helper)."""
    from clover_cli.auth import _file_lock

    lock_path = path.with_name(path.name + ".lock")
    with _file_lock(
        lock_path,
        _lock_holder,
        LOCK_TIMEOUT_SECONDS,
        f"timed out waiting for the restart-notice lock {lock_path}",
    ):
        yield


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to the restart-notice file")
        view = view[written:]


def append_rows(home: Path, rows: Iterable[dict]) -> int:
    """Append rows under the file lock; every byte is written before the fsync."""
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    if not payload:
        return 0
    path = file_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        data = payload.encode("utf-8")
        try:
            with path.open("rb") as existing:
                existing.seek(0, os.SEEK_END)
                if existing.tell() and (existing.seek(-1, os.SEEK_END) or existing.read(1)) != b"\n":
                    # A crashed writer left a torn last line; keep our row off it.
                    data = b"\n" + data
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(path.parent)
    return payload.count("\n")


def _quarantine(path: Path, bad_lines: list) -> None:
    corrupt = path.with_name(path.name + ".corrupt")
    fd = os.open(corrupt, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _write_all(fd, b"".join(line + b"\n" for line in bad_lines))
        os.fsync(fd)
    finally:
        os.close(fd)
    logger.warning(
        "Moved %d unreadable line(s) from %s to %s: %r",
        len(bad_lines), path, corrupt, [line[:200] for line in bad_lines],
    )


def _read_locked(path: Path) -> list[dict]:
    """Rows of the file; unreadable lines are moved aside.  Caller holds the lock."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    rows: list[dict] = []
    bad: list[bytes] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            bad.append(line)
            continue
        if isinstance(row, dict) and row.get("id") and row.get("chat_id") and row.get("platform"):
            rows.append(row)
        else:
            bad.append(line)
    if bad:
        _quarantine(path, bad)
        _rewrite(path, rows)
    return rows


def read_rows(home: Path) -> list[dict]:
    path = file_path(home)
    if not path.exists():
        return []
    with _locked(path):
        return _read_locked(path)


def _rewrite(path: Path, rows: list[dict]) -> None:
    """Replace the file with ``rows``.  Caller holds the lock."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def claim_rows(home: Path, ids: set) -> list[dict]:
    """Take rows out of the file by id and return only the ones this call took.

    Read, filter and replace happen under one lock, so a row is claimed by
    exactly one caller and a row appended meanwhile is never lost.  Only the
    returned rows may be sent.
    """
    path = file_path(home)
    if not path.exists():
        return []
    with _locked(path):
        rows = _read_locked(path)
        claimed = [row for row in rows if row["id"] in ids]
        if claimed:
            _rewrite(path, [row for row in rows if row["id"] not in ids])
        return claimed


def count_missed_startup(home: Path, ids: set) -> list[dict]:
    """Count one startup that could not deliver these rows; drop exhausted ones.

    Returns the rows that were dropped (already logged at WARNING).
    """
    path = file_path(home)
    if not path.exists():
        return []
    dropped: list[dict] = []
    with _locked(path):
        kept: list[dict] = []
        for row in _read_locked(path):
            if row["id"] in ids:
                row["attempts"] = int(row.get("attempts") or 0) + 1
                if row["attempts"] >= MAX_STARTUPS:
                    dropped.append(row)
                    continue
            kept.append(row)
        _rewrite(path, kept)
    for row in dropped:
        logger.warning(
            "Dropping a restart notice that could not be delivered after %d startups "
            "(%s:%s, message %r)",
            MAX_STARTUPS, row.get("platform"), row.get("chat_id"), row.get("preview"),
        )
    return dropped


def notice_text(rows: list[dict]) -> str:
    """One plain message covering every unhandled message of one chat."""
    shown = ", ".join(f'"{row["preview"]}"' for row in rows[:NOTICE_PREVIEWS])
    extra = len(rows) - NOTICE_PREVIEWS
    more = f" [+{extra} more]" if extra > 0 else ""
    return f"I restarted before handling your last message(s): {shown}{more}. Please send again."

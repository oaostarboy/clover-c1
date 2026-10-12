"""Durable record of every running background subagent of a gateway chat.

A gateway restart kills the daemon threads that run background subagents
(``delegate_task(background=true)``).  The async ledger (``async_delegation``)
already notices the dead owner and tells the parent "outcome unknown", but that
leaves the work undone.  This module keeps what is needed to START THE WORK
AGAIN after the restart: one small JSON file per dispatch under::

    <CLOVER_HOME>/cache/delegation/inflight/<delegation_id>.json   (0600)

holding the full task spec (goal / context / role / tier / model / title /
steps), where the chat is (platform, chat id, thread, session key, parent
session id), the card message id once the card is placed, and the paths the
interrupted child leaves behind (its worktree, its live transcript, its
session).  ``gateway.delegation_resume`` reads these on start and relaunches
each child ONCE.

The file is written at dispatch and removed on completion, failure or stop
(``async_delegation._finish_finalization``).  During a gateway shutdown the
interrupted children do NOT remove their file and do not report: the restart
picks them up (``hold_for_resume``).

Everything here is best-effort and never raises into the dispatch path.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

VERSION = 1
# A record older than this is reported to the parent instead of resumed.
MAX_RESUME_AGE_SECONDS = 6 * 3600.0
# A subagent is resumed once; the second interruption is reported.
MAX_RESUMES = 1

_LOCK = threading.RLock()

# Set (per thread of control) by the resume pass around its delegate_task call
# so the new dispatch's record carries the resume counter.
_RESUME_CONTEXT: contextvars.ContextVar = contextvars.ContextVar(
    "clover_delegation_resume_context", default=None
)

# True once the gateway began shutting down and interrupted its background
# children: from then on their completions are held for the restart.
_hold_for_resume: List[bool] = [False]


# ── config ────────────────────────────────────────────────────────────────

def resume_enabled() -> bool:
    """``delegation.resume_after_restart`` (default true)."""
    try:
        from clover_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        raw = (cfg.get("delegation") or {}).get("resume_after_restart", True)
    except Exception:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(raw) if raw is not None else True


# ── paths ─────────────────────────────────────────────────────────────────

def inflight_root() -> Path:
    from clover_constants import get_clover_dir

    return get_clover_dir("cache/delegation", "delegation_cache") / "inflight"


def _safe_id(delegation_id: Any) -> str:
    did = str(delegation_id or "").strip()
    if not did or any(c in did for c in "/\\\0") or did in (".", ".."):
        return ""
    return did


def record_path(delegation_id: Any) -> Optional[Path]:
    did = _safe_id(delegation_id)
    return (inflight_root() / f"{did}.json") if did else None


# ── read / write ──────────────────────────────────────────────────────────

def _write(path: Path, data: Dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, default=str)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return True
    except Exception:
        logger.debug("could not write inflight record %s", path, exc_info=True)
        return False


def read_record(delegation_id: Any) -> Optional[Dict[str, Any]]:
    path = record_path(delegation_id)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_records() -> List[Dict[str, Any]]:
    """Every readable record, oldest first.  Unreadable files are skipped."""
    out: List[Dict[str, Any]] = []
    try:
        files = sorted(inflight_root().glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("delegation_id"):
            out.append(data)
    out.sort(key=lambda r: float(r.get("dispatched_at") or 0))
    return out


def remove_record(delegation_id: Any) -> bool:
    path = record_path(delegation_id)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("could not remove inflight record %s", path, exc_info=True)
        return False


def update_record(delegation_id: Any, **fields: Any) -> bool:
    """Merge ``fields`` into an existing record (no record: nothing happens)."""
    path = record_path(delegation_id)
    if path is None:
        return False
    with _LOCK:
        data = read_record(delegation_id)
        if data is None:
            return False
        data.update(fields)
        return _write(path, data)


def _update_task(delegation_id: Any, task_index: int, **fields: Any) -> bool:
    path = record_path(delegation_id)
    if path is None:
        return False
    with _LOCK:
        data = read_record(delegation_id)
        if data is None:
            return False
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or not 0 <= int(task_index) < len(tasks):
            return False
        if not isinstance(tasks[int(task_index)], dict):
            return False
        tasks[int(task_index)].update(fields)
        return _write(path, data)


# ── the dispatch side ─────────────────────────────────────────────────────

def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return None


@contextlib.contextmanager
def resume_context(
    resumes: int, resumed_from: str, card: Optional[Dict[str, Any]] = None, work: str = "",
) -> Iterator[None]:
    """Mark dispatches made inside as the ``resumes``-th resume of ``resumed_from``;
    ``card`` is where the interrupted job's card message was (re-used if it still exists)."""
    token = _RESUME_CONTEXT.set({
        "resumes": int(resumes), "resumed_from": str(resumed_from), "card": card or None,
        "work": str(work or ""),
    })
    try:
        yield
    finally:
        _RESUME_CONTEXT.reset(token)


def _handoff_for_record(handoff: Any) -> Optional[Dict[str, Any]]:
    """The root's summary of the dispatch, without its opener line (a resumed
    card stands alone)."""
    if not isinstance(handoff, dict):
        return None
    keep = {
        k: v for k, v in handoff.items()
        if k in ("work", "outcome", "steps", "estimated_minutes_min", "estimated_minutes_max")
    }
    return _json_safe(keep) or None


def current_resume_card() -> Optional[Dict[str, Any]]:
    """The card message the job being resumed had, or None."""
    ctx = _RESUME_CONTEXT.get() or {}
    card = ctx.get("card")
    return card if isinstance(card, dict) and card.get("message_id") else None


def current_resume_work() -> str:
    """The card title the job being resumed had ('' = build one from its tasks)."""
    return str((_RESUME_CONTEXT.get() or {}).get("work") or "")


def _card_work(handoff: Any) -> str:
    """The card title the root gave the dispatch (``handoff.work``), or ''."""
    if not isinstance(handoff, dict):
        return ""
    return " ".join(str(handoff.get("work") or "").replace("*", "").split())[:120]


def write_dispatch_record(
    delegation_id: str,
    *,
    tasks: List[Dict[str, Any]],
    session_key: str,
    parent_session_id: Optional[str],
    origin: Dict[str, Any],
    top_context: Optional[str] = None,
    top_role: Optional[str] = None,
    model: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    handoff: Optional[Dict[str, Any]] = None,
    live_paths: Optional[List[str]] = None,
    fanout_delivery: str = "each",
    dispatched_at: Optional[float] = None,
    origin_session_id: str = "",
) -> bool:
    """Persist the spec of a background dispatch that a chat is waiting on.

    Only gateway chats are recorded (a routing origin with a platform and chat
    id): a CLI/TUI/cron dispatch has no gateway to restart it.  Off when
    ``delegation.resume_after_restart`` is false.  Returns True when written.
    """
    try:
        if not (origin.get("origin_platform") and origin.get("origin_chat_id")):
            return False
        if not session_key or not resume_enabled():
            return False
        ctx = _RESUME_CONTEXT.get() or {}
        owner_started = None
        try:
            from gateway.status import get_process_start_time

            owner_started = get_process_start_time(os.getpid())
        except Exception:
            owner_started = None
        data = {
            "version": VERSION,
            "delegation_id": str(delegation_id),
            "session_key": str(session_key),
            "parent_session_id": parent_session_id or "",
            "origin_session_id": origin_session_id or "",
            "origin": {k: v for k, v in origin.items() if isinstance(v, (str, int, float, bool))},
            "tasks": _json_safe(tasks) or [],
            "context": top_context,
            "role": top_role,
            "model": model,
            "toolsets": list(toolsets) if toolsets else None,
            "handoff": _handoff_for_record(handoff),
            "card_work": _card_work(handoff),
            "live_paths": list(live_paths or []),
            "fanout_delivery": fanout_delivery,
            "dispatched_at": float(dispatched_at or time.time()),
            "owner_pid": os.getpid(),
            "owner_started_at": owner_started,
            "resumes": int(ctx.get("resumes") or 0),
            "resumed_from": str(ctx.get("resumed_from") or ""),
            "card": None,
        }
        path = record_path(delegation_id)
        if path is None:
            return False
        with _LOCK:
            return _write(path, data)
    except Exception:
        logger.debug("could not record inflight dispatch", exc_info=True)
        return False


def note_worktree(delegation_id: Any, task_index: int, path: str) -> None:
    """The child's isolated worktree, so a resumed child can find its work."""
    if path:
        _update_task(delegation_id, task_index, worktree=str(path))


def note_child_session(delegation_id: Any, task_index: int, session_id: str) -> None:
    if session_id:
        _update_task(delegation_id, task_index, child_session_id=str(session_id))


def note_card(delegation_id: Any, *, platform: str, chat_id: str, thread_id: str, message_id: str) -> None:
    """Where the delegation's card message is (kept current as it is re-posted)."""
    if message_id:
        update_record(
            delegation_id,
            card={
                "platform": str(platform or ""), "chat_id": str(chat_id or ""),
                "thread_id": str(thread_id or ""), "message_id": str(message_id),
            },
        )


# ── shutdown hold ─────────────────────────────────────────────────────────

def begin_shutdown_hold() -> None:
    _hold_for_resume[0] = True


def end_shutdown_hold() -> None:
    _hold_for_resume[0] = False


def is_resumable(record: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    """Would a restart resume this record (flag on, not stale, resumes left)?"""
    if not record or not resume_enabled():
        return False
    now = time.time() if now is None else now
    if int(record.get("resumes") or 0) >= MAX_RESUMES:
        return False
    return (now - float(record.get("dispatched_at") or 0)) <= MAX_RESUME_AGE_SECONDS


def hold_for_resume(delegation_id: Any) -> bool:
    """True while the gateway is shutting down and ``delegation_id`` has a
    record a restart will resume: its interrupted completion must neither be
    reported nor remove the record."""
    if not _hold_for_resume[0]:
        return False
    return is_resumable(read_record(delegation_id))


def has_resumable_record(delegation_id: Any) -> bool:
    """Used by the abandoned-owner recovery: leave such a row to the resume."""
    return is_resumable(read_record(delegation_id))


def parent_session_alive(record: Optional[Dict[str, Any]]) -> bool:
    """Is the chat session that commissioned this job still there?

    Gone: no such session row, or it (and every compression continuation of
    it) ended for a reason other than compression -- ``/new``, a reset, a
    deleted conversation.  Unknown (state db unreadable): treated as alive, a
    resume is the safer mistake than silence.
    """
    pid = str((record or {}).get("parent_session_id") or "").strip()
    if not pid:
        return False
    try:
        from clover_state import SessionDB

        db = SessionDB()
        try:
            tip = db.get_compression_tip(pid) or pid
            row = db.get_session(tip) or db.get_session(pid)
            if row is None:
                return False
            ended = row.get("ended_at")
            reason = str(row.get("end_reason") or "")
            return not (ended and reason != "compression")
        finally:
            with contextlib.suppress(Exception):
                db.close()
    except Exception:
        logger.debug("could not check the parent session", exc_info=True)
        return True


def reset_for_tests() -> None:
    _hold_for_resume[0] = False

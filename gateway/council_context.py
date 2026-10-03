"""Record a council result the gateway already showed in chat into the conversation.

The live card posts its final answer straight through the adapter, so the
launching session's transcript never contains it and the next user turn
("explain better") is answered without it. After a card is shown, the gateway
appends ONE internal user row to the conversation the user is looking at.

Safety rules, all fail-closed:

* only the session recorded in ``origin.json`` (or its compression tip) that is
  still the key's current session receives the row; ``/new`` or a switch skips.
  The gateway's terminal children carry ``CLOVER_SESSION_KEY`` but no session
  id, so an origin without one is bound ONCE, when the watcher first sees the
  run, to the key's current session if that session was already live when the
  run started (``gateway-origin-session.json``);
* never while the session has a running agent or an active turn lease, because a
  row between ``assistant(tool_calls)`` and its ``tool`` rows corrupts replay;
* exactly once: ``gateway-context.json`` receipt plus a ``platform_message_id``
  dedupe against the transcript for a crash between the append and the receipt;
* a failure is logged and written as ``gateway-context-failed.json``, never
  reported as success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

CONTEXT_RECEIPT = "gateway-context.json"
CONTEXT_FAILED = "gateway-context-failed.json"
OBSERVED_SESSION = "gateway-origin-session.json"

_HEADER = (
    "[Council result already shown to the user in this chat — do not repost; "
    "explain or act on it if asked]"
)
_DELIVERY_LABEL = {
    "shown": "",
    "uncertain": " (delivery uncertain)",
    "not_shown": " (NOT shown to the user — share it)",
}


def _message_id(run_id: str) -> str:
    return f"council-context:{run_id}"


def format_context_row(
    kind: str,
    delivery: str,
    summary: Optional[Mapping[str, Any]],
    state: Optional[Mapping[str, Any]],
) -> str:
    """The text of the conversation row. ``kind`` is ``result`` or ``failure``."""
    head = _HEADER + _DELIVERY_LABEL.get(delivery, "")
    if kind != "result" or summary is None:
        stage = str((state or {}).get("stage") or "").replace("_", " ")
        return f"{head}\nThe council run failed" + (f" at the {stage} stage." if stage else ".")
    lines = [
        head,
        f"VERDICT: {summary.get('verdict') or 'No verdict returned.'}",
        f"WHY: {summary.get('why') or summary.get('next') or 'No reason returned.'}",
        f"CAVEAT: {summary.get('caveat') or summary.get('dissent') or 'No caveat returned.'}",
    ]
    severity = str(summary.get("attack_severity") or "").strip()
    if severity:
        lines.append(f"ATTACK: {severity} {summary.get('ruling') or ''}".rstrip())
    return "\n".join(lines)


def _write_json(path: Path, payload: Mapping[str, Any]) -> bool:
    try:
        path.write_text(json.dumps(dict(payload)), encoding="utf-8")
        return True
    except OSError:
        logger.warning("Could not write %s", path, exc_info=True)
        return False


class CouncilContextRecorder:
    """Appends the "already shown" row to the launching conversation."""

    def __init__(
        self,
        session_store: Any,
        *,
        is_running: Optional[Callable[[str], bool]] = None,
        poll_s: float = 1.0,
    ) -> None:
        self._store = session_store
        self._is_running = is_running
        self._poll_s = max(0.01, float(poll_s))

    async def record(
        self,
        *,
        origin: Mapping[str, Any],
        work: Path,
        kind: str,
        delivery: str,
        summary: Optional[Mapping[str, Any]],
        state: Optional[Mapping[str, Any]],
        deadline: float,
    ) -> str:
        """Write the row at the first idle moment. Returns the outcome.

        ``written`` / ``already`` / ``skipped`` are final. ``failed`` means the
        row is not in the conversation and ``gateway-context-failed.json`` says
        why. Waiting for an idle session is bounded only by ``deadline``.
        """
        work = Path(work)
        run_id = work.name
        if (work / CONTEXT_RECEIPT).exists():
            return "already"
        text = format_context_row(kind, delivery, summary, state)
        reason = "the session never became idle"
        while True:
            try:
                outcome, reason = await asyncio.to_thread(
                    self._attempt, origin, work, text
                )
            except Exception as exc:
                logger.warning("Council context for %s failed", run_id, exc_info=True)
                return self._failed(work, f"{type(exc).__name__}: {exc}")
            if outcome == "busy":
                if time.time() >= deadline:
                    return self._failed(work, reason)
                await asyncio.sleep(self._poll_s)
                continue
            break
        if outcome == "failed":
            return self._failed(work, reason)
        _write_json(
            work / CONTEXT_RECEIPT,
            {"status": outcome, "reason": reason, "run_id": run_id, "at": time.time()},
        )
        return outcome

    def _failed(self, work: Path, reason: str) -> str:
        logger.warning(
            "Council result for %s was shown in chat but is NOT in the conversation: %s",
            work.name, reason,
        )
        _write_json(
            work / CONTEXT_FAILED, {"reason": reason, "run_id": work.name, "at": time.time()}
        )
        return "failed"

    # -- one synchronous attempt -----------------------------------------------

    def observe_origin_session(self, origin: Mapping[str, Any], work: Path) -> None:
        """Stamp, once, which session an origin without a ``session_id`` belongs to.

        Bound only when the key's current session started at or before the run
        and has not ended; otherwise ``session_id`` is ``None`` and the run is
        skipped. Called when the watcher first discovers the run.
        """
        work = Path(work)
        path = work / OBSERVED_SESSION
        key = str(origin.get("session_key") or "")
        if origin.get("session_id") or not key or path.exists():
            return
        session_id, reason = None, ""
        try:
            current = self._store.peek_session_id(key)
            row = self._store._db.get_session(current) if current else None
            launched_at = float(origin.get("created_at") or 0)
            if not row:
                reason = "the origin session key does not resolve to a session"
            elif row.get("ended_at"):
                reason = "the current session had already ended"
            elif not launched_at or float(row.get("started_at") or 0) > launched_at:
                reason = "the current session started after the run did"
            else:
                session_id = current
        except Exception as exc:
            logger.warning("Council %s: could not observe the origin session", work.name, exc_info=True)
            reason = f"lookup failed: {type(exc).__name__}"
        if session_id is None:
            logger.info("Council %s: origin session not bound: %s", work.name, reason)
        _write_json(
            path, {"session_id": session_id, "reason": reason, "observed_at": time.time()}
        )

    def _launched_session_id(self, origin: Mapping[str, Any], work: Path) -> str:
        launched = str(origin.get("session_id") or "")
        if launched:
            return launched
        try:
            observed = json.loads((Path(work) / OBSERVED_SESSION).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(observed.get("session_id") or "") if isinstance(observed, dict) else ""

    def _resolve_target(
        self, origin: Mapping[str, Any], work: Path
    ) -> tuple[Optional[str], str, set]:
        """The session that may receive the row, or ``None`` and why not."""
        store = self._store
        key = str(origin.get("session_key") or "")
        launched = self._launched_session_id(origin, work)
        if not launched:
            return None, "origin has no session_id and none could be observed", set()
        if not key:
            return None, "origin has no session_key", set()
        current = store.peek_session_id(key)
        if not current:
            return None, "the origin session key no longer resolves to a session", set()
        launched_tip = store._compression_tip_for_session_id(launched)
        if current not in (launched, launched_tip):
            return None, "the chat moved to another session (/new or a switch)", set()
        target = store._compression_tip_for_session_id(current)
        return target, "", {launched, current, target}

    def _attempt(self, origin: Mapping[str, Any], work: Path, text: str) -> tuple[str, str]:
        store = self._store
        run_id = work.name
        target, reason, lineage = self._resolve_target(origin, work)
        if target is None:
            logger.info("Council %s: not recording in a conversation: %s", run_id, reason)
            return "skipped", reason
        db = getattr(store, "_db", None)
        if db is None:
            return "failed", "no session database"
        message_id = _message_id(run_id)
        if any(store.has_platform_message_id(sid, message_id) for sid in lineage):
            return "already", "row already in the transcript"
        key = str(origin.get("session_key") or "")
        if self._is_running is not None and self._is_running(key):
            return "busy", "the session has a running agent"
        holder = f"pid={os.getpid()} council-context:{run_id}"
        if not db.try_acquire_session_turn_lease(target, holder, ttl_seconds=30.0):
            return "busy", "the session holds an active turn lease"
        try:
            store.append_to_transcript(
                target,
                {
                    "role": "user",
                    "content": text,
                    "display_kind": "internal_notification",
                    "platform_message_id": message_id,
                },
            )
        finally:
            db.release_session_turn_lease(target, holder)
        # append_to_transcript queues and logs on a DB error instead of raising.
        landed = {target, store._compression_tip_for_session_id(target)}
        if not any(store.has_platform_message_id(sid, message_id) for sid in landed):
            return "failed", "the transcript append did not persist"
        return "written", f"appended to {target}"

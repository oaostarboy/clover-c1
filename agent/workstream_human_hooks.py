"""Trusted, bounded human correction and feedback ingress for workstreams."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from agent import workstreams
from cron import notepad

logger = logging.getLogger(__name__)
_MAX_MESSAGE_BYTES = 8192
_FEEDBACK = re.compile(
    r"^\s*/feedback\s+(ws_[0-9a-f]{16})\s+"
    r"(accept|reject|correct|defer|done|stop)"
    r"(?:\s*:\s*(.*))?\s*$",
    re.IGNORECASE | re.DOTALL,
)



@dataclass(frozen=True)
class HumanHookResult:
    handled: bool
    correction_id: Optional[str]
    feedback_id: Optional[str]
    response_text: str


def _no_result() -> HumanHookResult:
    return HumanHookResult(False, None, None, "")


def _authenticated_input(agent, *, role: str, message: str, session_id: str,
                        message_id: Optional[str]):
    """Read back the central owner's trusted ingress receipt for this row."""
    if role != "user" or not isinstance(message, str) or not message.strip():
        return None
    if len(message.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        return None
    if not session_id or str(getattr(agent, "session_id", "") or "") != session_id:
        return None
    # The central SessionDB owner has not published its reader in every
    # dependency checkout yet. A user role, TTY, row ID or marker is not a
    # substitute: absent the owner-issued read API, fail closed without writes.
    reader = getattr(workstreams, "_HUMAN_ORIGIN_READER", None)
    verify_origin = getattr(workstreams, "verified_human_origin", None)
    if not callable(reader) or not callable(verify_origin):
        return None
    db = None
    keep_open = False
    try:
        from clover_state import SessionDB

        db = SessionDB(read_only=True)
    except Exception:
        logger.debug("human workstream ingress unavailable; refusing event", exc_info=True)
        return None
    try:
        session = db.get_session(session_id)
        if not session:
            return None
        platform = str(getattr(agent, "platform", "") or "")
        if not platform or str(session.get("source") or "") != platform:
            return None
        for attr, stored in (("_user_id", "user_id"), ("_chat_id", "chat_id"), ("_chat_type", "chat_type")):
            caller_value = str(getattr(agent, attr, "") or "")
            stored_value = str(session.get(stored) or "")
            if caller_value != stored_value:
                return None
        rows = db.get_messages(session_id, limit=1, latest=True)
        if not rows:
            return None
        row = rows[0]
        if row.get("role") != "user" or row.get("tool_call_id"):
            return None
        if row.get("display_kind") not in (None, "user"):
            return None
        if row.get("content") != message:
            return None
        stored_message_id = str(row.get("message_id") or row.get("platform_message_id") or "")
        row_id = str(row.get("id") or "")
        if message_id is not None and str(message_id) not in {stored_message_id, row_id}:
            return None
        if stored_message_id and message_id is not None and str(message_id) != stored_message_id:
            # The caller may use the durable SessionDB row id only where no
            # transport message ID exists; never substitute it for a known ID.
            return None
        if not row_id:
            return None
        origin = reader(db, session_id, row["id"])
        if origin is None or str(getattr(origin, "session_id", "")) != session_id:
            return None
        if str(getattr(origin, "message_row_id", "")) != row_id:
            return None
        verified = verify_origin(origin, db=db, human_reader=reader)
        if verified is None:
            return None
        keep_open = True
        return db, row, verified, reader
    except Exception:
        logger.debug("trusted human-origin read failed closed", exc_info=True)
        return None
    finally:
        if db is not None and not keep_open:
            db.close()


def _feedback_request(message: str):
    match = _FEEDBACK.match(message)
    if not match:
        return None
    return match.group(1), match.group(2).lower(), (match.group(3) or "").strip()


def consume_human_workstream_message(
    agent, *, role: str, message: str, session_id: str,
    message_id: Optional[str] = None, now: Optional[datetime] = None,
) -> HumanHookResult:
    """Persist explicit input only with an owner-issued human-origin receipt.

    Role, TTY, text, and caller-supplied IDs are not provenance. The exact
    current SessionDB row must have a matching, unconsumed receipt from the
    central owner; absent that published API this is a strict no-op.
    """
    if role != "user":
        return _no_result()
    try:
        settings = workstreams.load_settings()
        if not settings.enabled:
            return _no_result()
        scope = workstreams.scope_for_agent(agent)
    except Exception:
        logger.debug("workstream settings/scope unavailable; refusing human event", exc_info=True)
        return _no_result()
    authenticated = _authenticated_input(
        agent, role=role, message=message, session_id=session_id, message_id=message_id,
    )
    if authenticated is None:
        return _no_result()
    db, _, origin, reader = authenticated

    try:
        correction_text = workstreams.explicit_correction_from_message(message)
        if correction_text is not None:
            capture: Any = workstreams.capture_user_correction
            row = capture(
                scope, session_id=session_id, settings=settings, db=db,
                human_reader=reader, now=now,
            )
            if row is None:
                return _no_result()
            return HumanHookResult(True, str(row["workstream_id"]), None,
                                   "Recorded as a scoped correction.")

        feedback = _feedback_request(message)
        if feedback is None:
            return _no_result()
        workstream_id, kind, note = feedback
        if not settings.suggestions_enabled:
            return _no_result()
        if not notepad.get_workstream(scope, workstream_id):
            return _no_result()
        record_feedback: Any = workstreams.record_feedback
        row = record_feedback(
            scope, workstream_id, kind=kind, settings=settings, origin=origin,
            human_reader=reader, db=db, note=note, now=now,
        )
        if row is not None:
            return HumanHookResult(True, None, str(row["feedback_id"]),
                                   "Recorded as scoped feedback.")
        return _no_result()
    except Exception:
        # Owner failures, missing scope targets, and persistence errors fail
        # closed; never turn a failed write into an acknowledgement or action.
        logger.debug("human workstream event was not persisted", exc_info=True)
        return _no_result()
    finally:
        db.close()

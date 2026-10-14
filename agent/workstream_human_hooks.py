"""Host consumer of the trusted human-origin receipt for workstreams.

Runs inside the real agent turn, right after this turn's user row was
persisted (so the SessionDB receipt exists) and before the model is called.
It turns explicit human text into scoped workstream corrections / feedback and
then consumes the receipt exactly once.

Authority model (provenance only, never execution permission):

* the *active invocation capability* (``agent._trusted_human_ingress``, minted
  by the authenticated adapter and redeemed by the gateway) must still be
  valid: sealed, same profile, and its gateway run generation still current
  (Stop / /new / cancel fence);
* the persisted receipt read back through ``read_human_origin`` must belong to
  *this* invocation (same ingress event id), this session, and the exact
  persisted user row whose content equals the message;
* the session root must equal the invocation's root;
* the scope is the canonical ``scope_for_agent`` shared with the recall
  consumers.

A role, TTY, marker, row id, model/tool text or a historical receipt is never
evidence. Several explicit operations in one event are applied individually
idempotently (keyed by the ingress event id), then the receipt is consumed
once, after every operation reached a settled outcome.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional, Tuple

from agent import workstreams
from agent import workstream_origin
from cron import notepad

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 8192
MAX_OPERATIONS = 8
CONSUMER = "workstream_human_hooks"

_FEEDBACK = re.compile(
    r"^\s*(?:/feedback\s+|feedback\s*:\s*)(ws_[0-9a-f]{16})\s+"
    r"(accept|reject|correct|defer|done|stop)"
    r"(?:\s*:\s*(.*?))?\s*$",
    re.IGNORECASE,
)
_CORRECTION = re.compile(
    r"^\s*(?:/correct(?:ion)?(?:\s*:\s*|\s+)|correction\s*:\s*)(\S.*?)\s*$",
    re.IGNORECASE,
)
_TARGET = re.compile(r"^(ws_[0-9a-f]{16})\s*:?\s+(\S.*)$", re.DOTALL)


@dataclass(frozen=True)
class Operation:
    kind: str  # "correction" | "feedback"
    target: Optional[str]
    text: str  # correction text, or feedback note
    feedback_kind: str = ""


@dataclass(frozen=True)
class OperationResult:
    index: int
    kind: str
    status: str  # applied | already_applied | denied | error
    reason: str = ""
    workstream_id: Optional[str] = None
    feedback_id: Optional[str] = None


@dataclass(frozen=True)
class HumanHookResult:
    handled: bool
    correction_id: Optional[str]
    feedback_id: Optional[str]
    response_text: str
    consumed: bool = False
    event_id: Optional[str] = None
    operations: Tuple[OperationResult, ...] = field(default_factory=tuple)


def _no_result() -> HumanHookResult:
    return HumanHookResult(False, None, None, "")


def parse_operations(message: Any) -> Optional[List[Operation]]:
    """Explicit operations of one human message, in order, else ``None``.

    The first non-empty line must itself be a marker (nothing is inferred
    from prose). Later marker lines start further operations; any other line
    continues the preceding correction's text.
    """
    if not isinstance(message, str) or not message.strip():
        return None
    ops: List[Operation] = []
    lines = message.splitlines()
    for line in lines:
        if not line.strip() and not ops:
            continue
        feedback = _FEEDBACK.match(line)
        if feedback:
            ops.append(Operation("feedback", feedback.group(1), (feedback.group(3) or "").strip(),
                                 feedback.group(2).lower()))
            continue
        correction = _CORRECTION.match(line)
        if correction:
            body = correction.group(1)
            target = _TARGET.match(body)
            if target:
                ops.append(Operation("correction", target.group(1), target.group(2).strip()))
            else:
                ops.append(Operation("correction", None, body))
            continue
        if not ops:
            return None  # first content line is not an explicit marker
        last = ops[-1]
        if last.kind == "correction" and line.strip():
            ops[-1] = Operation("correction", last.target, last.text + "\n" + line.strip())
    return ops or None


def _authenticate(agent, *, role: str, message: str, session_id: str,
                  message_id: Optional[str], row_id: Any):
    """``(db, row, origin, ingress)`` only when every provenance fact holds."""
    if role != "user" or not isinstance(message, str) or not message.strip():
        return None
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        return None
    if not session_id or str(getattr(agent, "session_id", "") or "") != session_id:
        return None
    ingress = getattr(agent, "_trusted_human_ingress", None)
    if not workstream_origin.ingress_is_valid(ingress, profile=workstreams.current_profile()):
        return None  # unsealed / foreign profile / retired / Stop-cancelled
    db = getattr(agent, "_session_db", None)
    if db is None or not callable(getattr(db, "get_human_origin", None)):
        return None
    try:
        if db.get_session(session_id) is None:
            return None
        if db.get_conversation_root(session_id) != db.get_conversation_root(ingress.session_id):
            return None
        rows = db.get_messages(session_id, limit=20, latest=True)
        if row_id is None:
            row = rows[-1] if rows else None
        else:
            row = next((r for r in rows if str(r.get("id")) == str(row_id)), None)
        if row is None or row.get("role") != "user" or row.get("tool_call_id"):
            return None
        if row.get("display_kind") not in (None, "user"):
            return None
        if row.get("content") != message:
            return None
        stored_id = str(row.get("message_id") or row.get("platform_message_id") or "")
        if message_id is not None and stored_id and str(message_id) != stored_id:
            return None
        origin = workstream_origin.read_human_origin(db, session_id, row["id"])
        if origin is None or origin.ingress_event_id != ingress.event_id:
            return None
        if origin.session_id != session_id or str(origin.message_row_id) != str(row["id"]):
            return None
        verified = workstreams.verified_human_origin(
            origin, db=db, human_reader=workstream_origin.read_human_origin)
        if verified is None:
            return None
        return db, row, verified, ingress
    except Exception:
        logger.debug("trusted human-origin read failed closed", exc_info=True)
        return None


def _apply(index: int, op: Operation, *, scope, settings, db, origin, session_id, now):
    reader = workstream_origin.read_human_origin
    try:
        if op.kind == "correction":
            if not op.text.strip():
                return OperationResult(index, op.kind, "denied", "empty correction")
            if op.target and notepad.get_workstream(scope, op.target) is None:
                return OperationResult(index, op.kind, "denied",
                                       f"unknown workstream {op.target} in this scope")
            row = workstreams.record_correction(
                scope, text=op.text, settings=settings, origin=origin, human_reader=reader,
                db=db, supersedes_id=op.target, session_id=session_id, approved=True, now=now)
            if row is None:
                return OperationResult(index, op.kind, "denied", "scope not eligible")
            return OperationResult(index, op.kind, "applied", workstream_id=str(row["workstream_id"]))
        if not settings.suggestions_enabled:
            return OperationResult(index, op.kind, "denied", "suggestions are off")
        if notepad.get_workstream(scope, op.target) is None:
            return OperationResult(index, op.kind, "denied",
                                   f"unknown workstream {op.target} in this scope")
        row = workstreams.record_feedback(
            scope, op.target, kind=op.feedback_kind, settings=settings, origin=origin,
            human_reader=reader, db=db, note=op.text, now=now)
        if row is None:
            return OperationResult(index, op.kind, "denied", "scope not eligible")
        return OperationResult(index, op.kind, "applied", workstream_id=op.target,
                               feedback_id=str(row["feedback_id"]))
    except (notepad.WorkstreamError, ValueError) as exc:
        # Known policy denials (cap, stale revision, bad value): nothing was
        # acknowledged as success.
        return OperationResult(index, op.kind, "denied", f"{type(exc).__name__}: {exc}"[:200])
    except Exception:
        logger.warning("human workstream operation failed", exc_info=True)
        return OperationResult(index, op.kind, "error", "write failed; not recorded")


def _report(results: List[OperationResult]) -> str:
    lines = []
    for r in results:
        if r.status in ("applied", "already_applied"):
            what = "correction" if r.kind == "correction" else "feedback"
            lines.append(f"Recorded {what} {r.workstream_id}.")
        else:
            lines.append(f"Not recorded ({r.kind} {r.index + 1}): {r.reason}.")
    return " ".join(lines)


def consume_human_workstream_message(
    agent, *, role: str, message: str, session_id: str,
    message_id: Optional[str] = None, now: Optional[datetime] = None,
    row_id: Any = None,
) -> HumanHookResult:
    """Apply explicit human operations of the just-persisted user row."""
    if role != "user":
        return _no_result()
    try:
        settings = workstreams.load_settings()
        if not settings.enabled:
            return _no_result()
        scope = workstreams.scope_for_agent(agent)
        if settings.private_only and not scope.private:
            return _no_result()
    except Exception:
        logger.debug("workstream settings/scope unavailable; refusing human event", exc_info=True)
        return _no_result()
    ops = parse_operations(message)
    if not ops:
        return _no_result()
    authenticated = _authenticate(
        agent, role=role, message=message, session_id=session_id,
        message_id=message_id, row_id=row_id,
    )
    if authenticated is None:
        return _no_result()
    db, row, origin, ingress = authenticated
    if len(ops) > MAX_OPERATIONS:
        return HumanHookResult(
            False, None, None,
            f"Not recorded: more than {MAX_OPERATIONS} operations in one message.",
            event_id=ingress.event_id,
        )
    results = [
        _apply(i, op, scope=scope, settings=settings, db=db, origin=origin,
               session_id=session_id, now=now)
        for i, op in enumerate(ops)
    ]
    settled = all(r.status != "error" for r in results)
    consumed = False
    if settled and any(r.status in ("applied", "already_applied") for r in results):
        # Effects are committed above; only now is the receipt spent.
        try:
            consumed = bool(db.consume_human_origin(session_id, row["id"], consumer=CONSUMER))
        except Exception:
            logger.warning("human-origin consume failed after effects", exc_info=True)
    applied = [r for r in results if r.status in ("applied", "already_applied")]
    corr = next((r.workstream_id for r in applied if r.kind == "correction"), None)
    fb = next((r.feedback_id for r in applied if r.kind == "feedback"), None)
    return HumanHookResult(
        bool(applied), corr, fb, _report(results), consumed=consumed,
        event_id=ingress.event_id, operations=tuple(results),
    )


def consume_persisted_turn(agent, *, message: Any, row_id: Any) -> HumanHookResult:
    """Prologue entry: the exact just-persisted current-turn user row."""
    if not isinstance(message, str):
        return _no_result()
    return consume_human_workstream_message(
        agent, role="user", message=message,
        session_id=str(getattr(agent, "session_id", "") or ""), row_id=row_id,
    )

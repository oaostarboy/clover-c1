"""User notices for background results that never reached the assistant (I2).

``SessionDB.inbox_drop`` queues a notice (``notice_state='pending'``) unless
the user already saw the content. The sweeper sends exactly one short message
per record, to that record's OWN route, and never sends one twice:

``pending`` -> ``uncertain`` (claimed BEFORE the send, so a crash mid-send can
never produce a second message) -> ``sent`` on a confirmed send. Once a send
was ATTEMPTED the record stays ``uncertain`` whatever happens next — an
exception, a timeout, or an adapter that reports ``success=False`` (adapters
collapse post-send errors into that result, so it proves nothing). Only a
sender that decided, BEFORE calling the transport, that nothing could be sent
(:class:`NoticeNotSent`: no adapter, not connected) puts it back to
``pending``. ``/results`` lists ``uncertain`` records too.

Records that cannot be sent yet are retried with per-key exponential backoff
(15 s -> 1 h) and the sweep pages past them by ``seq``, so a pile of
unsendable notices never starves a deliverable one behind them.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

NOTICE_TEXT = (
    "Background result not delivered to the assistant: {title}. "
    "Use /results in that conversation (resume it with /resume if you started a new one)."
)
_TITLE_MAX = 80
_DEFAULT_TITLE = "background task"
BACKOFF_INITIAL_S = 15.0
BACKOFF_MAX_S = 3600.0
_BACKOFF_MAX_KEYS = 10_000


class NoticeNotSent(Exception):
    """Raised BEFORE any transport call: nothing was, or could be, sent."""


@dataclass
class NoticeSweepState:
    """In-memory pacing for one sweeper (per database): paging cursor + backoff."""

    cursor: int = 0
    # Wall-clock high-water mark of the dropped-delegation repair scan.
    repair_hwm: float = 0.0
    # key -> (next monotonic time to try, current delay)
    deferred: Dict[str, tuple] = field(default_factory=dict)


def notice_text(record: Dict[str, Any], redact: Optional[Callable[[str], str]] = None) -> str:
    title = " ".join(str(record.get("title") or "").split())
    if redact is not None and title:
        title = redact(title)
    title = title.strip()
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1].rstrip() + "…"
    return NOTICE_TEXT.format(title=title or _DEFAULT_TITLE)


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def sweep_inbox_notices(
    db: Any,
    send: Callable[[Dict[str, Any], str], Awaitable[None]],
    *,
    redact: Optional[Callable[[str], str]] = None,
    limit: int = 50,
    state: Optional[NoticeSweepState] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, int]:
    """Send each owed notice once. ``send(record, text)`` delivers to the
    record's own route and returns on success; it raises :class:`NoticeNotSent`
    only when it can prove nothing was sent, and anything else after an attempt.
    """
    state = state if state is not None else NoticeSweepState()
    counts = {"sent": 0, "uncertain": 0, "deferred": 0}
    now = clock()
    records = await _await(db.inbox_pending_notices(limit, state.cursor))
    for record in records:
        key = record["key"]
        due = state.deferred.get(key)
        if due is not None and due[0] > now:
            continue
        # Claim first: from here a crash or error can only under-send.
        if not await _await(db.inbox_set_notice_state(key, "uncertain", "pending")):
            continue
        try:
            await send(record, notice_text(record, redact))
        except NoticeNotSent as exc:
            logger.debug("Inbox notice %s deferred: %s", key, exc)
            await _await(db.inbox_set_notice_state(key, "pending", "uncertain"))
            delay = min(BACKOFF_MAX_S, max(BACKOFF_INITIAL_S, (due[1] * 2) if due else BACKOFF_INITIAL_S))
            if len(state.deferred) >= _BACKOFF_MAX_KEYS:
                state.deferred.clear()
            state.deferred[key] = (clock() + delay, delay)
            counts["deferred"] += 1
        except Exception:
            logger.warning(
                "Inbox notice %s may or may not have been sent; not retrying",
                key, exc_info=True,
            )
            state.deferred.pop(key, None)
            counts["uncertain"] += 1
        else:
            await _await(db.inbox_set_notice_state(key, "sent", "uncertain"))
            state.deferred.pop(key, None)
            counts["sent"] += 1
    # Page forward past this window; wrap once the end of the queue is reached.
    state.cursor = int(records[-1]["seq"]) if len(records) >= limit else 0
    return counts

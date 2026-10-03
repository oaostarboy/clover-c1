"""User notices for background results that never reached the assistant (I2).

``SessionDB.inbox_drop`` queues a notice (``notice_state='pending'``) unless
the user already saw the content. Notices pending together for one route are
swept as ONE short message (``Background results not delivered ...: a; b; c
(+N more)``), sent to that route itself, and never sent twice:

``pending`` -> ``uncertain`` (every included record is claimed BEFORE the send,
so a crash mid-send can never produce a second message) -> ``sent`` on a
confirmed send. Once a send was ATTEMPTED all of its records stay ``uncertain``
whatever happens next — an exception, a timeout, or an adapter that reports
``success=False`` (adapters collapse post-send errors into that result, so it
proves nothing). Only a
sender that decided, BEFORE calling the transport, that nothing could be sent
(:class:`NoticeNotSent`: no adapter, not connected) puts them back to
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
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

NOTICE_TEXT = (
    "Background result not delivered to the assistant: {title}. "
    "Use /results in that conversation (resume it with /resume if you started a new one)."
)
NOTICES_TEXT = (
    "Background results not delivered to the assistant: {titles}. "
    "Use /results in that conversation (resume it with /resume if you started a new one)."
)
_TITLE_MAX = 80
_GROUP_TITLE_MAX = 60
_GROUP_TITLES_SHOWN = 3
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


def _clean_title(
    record: Dict[str, Any], redact: Optional[Callable[[str], str]], limit: int
) -> str:
    title = " ".join(str(record.get("title") or "").split())
    if redact is not None and title:
        title = redact(title)
    title = title.strip()
    if len(title) > limit:
        title = title[: limit - 1].rstrip() + "…"
    return title or _DEFAULT_TITLE


def notice_text(record: Dict[str, Any], redact: Optional[Callable[[str], str]] = None) -> str:
    return NOTICE_TEXT.format(title=_clean_title(record, redact, _TITLE_MAX))


def group_notice_text(
    records: List[Dict[str, Any]], redact: Optional[Callable[[str], str]] = None
) -> str:
    """One message for several records on the same route: three titles at most."""
    if len(records) == 1:
        return notice_text(records[0], redact)
    shown = [
        _clean_title(r, redact, _GROUP_TITLE_MAX) for r in records[:_GROUP_TITLES_SHOWN]
    ]
    titles = "; ".join(shown)
    more = len(records) - len(shown)
    if more > 0:
        titles += f" (+{more} more)"
    return NOTICES_TEXT.format(titles=titles)


def _route_group_key(record: Dict[str, Any]) -> tuple:
    """Records that land in the same chat/topic share a key. A record with no
    recorded route is recovered per owner at send time, so it groups by owner."""
    if record.get("platform") and record.get("chat_id"):
        return (
            "route", record.get("profile"), record["platform"],
            str(record["chat_id"]), record.get("thread_id") or None,
        )
    return ("owner", record.get("owner_root_id") or record["key"])


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
    """Send each owed notice once, one message per route. ``send(record, text)``
    delivers to the route of *record* (the first of the group) and returns on
    success; it raises :class:`NoticeNotSent` only when it can prove nothing was
    sent, and anything else after an attempt. Every record in the message is
    claimed first and shares the outcome.
    """
    state = state if state is not None else NoticeSweepState()
    counts = {"sent": 0, "uncertain": 0, "deferred": 0}
    now = clock()
    records = await _await(db.inbox_pending_notices(limit, state.cursor))
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for record in records:
        due = state.deferred.get(record["key"])
        if due is not None and due[0] > now:
            continue
        groups.setdefault(_route_group_key(record), []).append(record)
    for group in groups.values():
        # Claim first: from here a crash or error can only under-send.
        claimed = [
            r for r in group
            if await _await(db.inbox_set_notice_state(r["key"], "uncertain", "pending"))
        ]
        if not claimed:
            continue
        try:
            await send(claimed[0], group_notice_text(claimed, redact))
        except NoticeNotSent as exc:
            logger.debug("Inbox notice(s) %s deferred: %s", [r["key"] for r in claimed], exc)
            for record in claimed:
                key = record["key"]
                due = state.deferred.get(key)
                await _await(db.inbox_set_notice_state(key, "pending", "uncertain"))
                delay = min(BACKOFF_MAX_S, max(BACKOFF_INITIAL_S, (due[1] * 2) if due else BACKOFF_INITIAL_S))
                if len(state.deferred) >= _BACKOFF_MAX_KEYS:
                    state.deferred.clear()
                state.deferred[key] = (clock() + delay, delay)
                counts["deferred"] += 1
        except Exception:
            logger.warning(
                "Inbox notice(s) %s may or may not have been sent; not retrying",
                [r["key"] for r in claimed], exc_info=True,
            )
            for record in claimed:
                state.deferred.pop(record["key"], None)
                counts["uncertain"] += 1
        else:
            for record in claimed:
                await _await(db.inbox_set_notice_state(record["key"], "sent", "uncertain"))
                state.deferred.pop(record["key"], None)
                counts["sent"] += 1
    # Page forward past this window; wrap once the end of the queue is reached.
    state.cursor = int(records[-1]["seq"]) if len(records) >= limit else 0
    return counts

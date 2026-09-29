"""Best-effort removal of a turn's temporary progress bubbles.

Platform adapters report API failure through their *return value*
(``edit_message`` -> ``SendResult(success=False)``, ``delete_message`` ->
``False``), not by raising. Cleanup that only wraps calls in ``try/except``
therefore silently leaves bubbles behind on a flaky connection — the
"⏳ Working — 3 min" note that stayed in the chat after the reply landed.
These helpers check the return values, retry briefly, and fall back from a
failed card edit to deleting the bubble.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, List, Optional

from gateway.platforms.base import BasePlatformAdapter

logger = logging.getLogger(__name__)

# Seconds to wait before each retry after a failed edit/delete.
_RETRY_DELAYS = (0.5, 1.5)


def _supports_delete(adapter: Any) -> bool:
    return getattr(type(adapter), "delete_message", None) is not BasePlatformAdapter.delete_message


async def delete_bubble(adapter: Any, chat_id: str, message_id: str) -> bool:
    """Delete one bubble, retrying failures. Returns True when it is gone."""
    delays = _RETRY_DELAYS if _supports_delete(adapter) else ()
    for attempt in range(len(delays) + 1):
        try:
            deleted = await adapter.delete_message(chat_id, message_id)
        except Exception:
            deleted = False
        if deleted is not False:
            return True
        if attempt < len(delays):
            await asyncio.sleep(delays[attempt])
    logger.debug("progress cleanup: could not delete bubble %s", message_id)
    return False


async def _edit_into_card(adapter: Any, chat_id: str, message_id: str, card_text: str) -> bool:
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            # finalize=True is REQUIRED: without it the adapter takes the
            # streaming branch and sends the card with NO parse_mode, so it
            # arrives as literal "**> ... ||" markup.
            result = await adapter.edit_message(
                chat_id, message_id, card_text, finalize=True,
            )
        except Exception:
            # A raised error is not a transient API failure; don't retry.
            return False
        if result is None or getattr(result, "success", True):
            return True
        if attempt < len(_RETRY_DELAYS):
            await asyncio.sleep(_RETRY_DELAYS[attempt])
    return False


async def collapse_or_delete(
    adapter: Any,
    chat_id: str,
    ids: Iterable[str],
    *,
    card_text: str = "",
    can_card: bool = False,
    extra_ids: Optional[Iterable[str]] = None,
) -> None:
    """Edit the first bubble into the summary card and delete the rest, or
    delete everything. ``extra_ids`` are bubbles that were tracked after the
    caller snapshotted ``ids`` (e.g. a heartbeat that raced the final reply).

    A bubble is only kept when it was successfully turned into the card, so a
    stale progress note can never be left behind by a failed edit.
    """
    ordered: List[str] = list(ids)
    for mid in extra_ids or ():
        if mid not in ordered:
            ordered.append(mid)
    keep = None
    if can_card and ordered:
        keep = ordered[0]
        if not await _edit_into_card(adapter, chat_id, keep, card_text):
            keep = None
    for mid in ordered:
        if mid == keep:
            continue
        await delete_bubble(adapter, chat_id, mid)

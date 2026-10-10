"""Telegram update-ID admission ahead of every PTB handler group.

Telegram redelivers an update whose acknowledgement (the next getUpdates offset,
or the cleanup call in Updater.stop) never landed. That happens after a crash,
a reconnect, or an adapter rebuild, where the replacement adapter starts with an
empty in-memory set and a new PTB Updater polls from offset 0.

This gate runs in PTB group -1, before any core or plugin handler. An update ID
that was already claimed is answered once: a replay raises ApplicationHandlerStop
so no later group sees it. A claim is only an in-memory in-flight marker. It
becomes a persisted per-bot receipt under CLOVER_HOME (so a rebuilt adapter or a
restarted gateway still drops the replay) once the inbound work is durably handed
off: the event's turn marker is set, it is recorded in the restart inbox, or its
turn finished. A crash before that leaves no receipt, so Telegram's unacknowledged
replay is processed rather than lost. Updates that build no event are completed
by a final-group handler.

Adapted from NousResearch/hermes-agent plugins/platforms/telegram/update_admission.py
(MIT), commits 992f569fc1 and a1838ea87a. The Hermes version subclasses PTB's
Application to pin dispatch claims and flushes receipts from a background task.
This port uses a plain group -1 handler with PTB's own ApplicationHandlerStop, and
writes each receipt synchronously when the update's work is handed off. The receipt-file layout and 24h TTL are
kept from Hermes.

No cross-process coordination or exactly-once effects are promised.
"""

import json
import logging
import time

from telegram.ext import ApplicationHandlerStop

from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Bounded admitted history, in memory and on disk.
_SEEN_CAP = 4096
# The Bot API keeps an unconfirmed update for at most 24 hours, so an older
# receipt can never match a redelivery. It is also well inside the week of
# silence after which Telegram may restart update IDs at a random value.
RECEIPT_TTL_SECONDS = 24 * 60 * 60
ADMISSION_GROUP = -1
FINALIZE_GROUP = 100


def _receipt_path(receipt_dir, bot_id):
    return receipt_dir / f"telegram_update_receipts_{bot_id}.json"


def load_receipts(seen: dict, receipt_dir, bot_id) -> None:
    """Seed admitted history for one bot from its receipt file."""
    path = _receipt_path(receipt_dir, bot_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logger.warning("[Telegram] Ignoring unreadable update receipts at %s", path, exc_info=True)
        return
    ids = payload.get("update_ids") if isinstance(payload, dict) else None
    if not isinstance(ids, dict):
        return
    cutoff = time.time() - RECEIPT_TTL_SECONDS
    for uid, ts in ids.items():
        if not isinstance(ts, (int, float)) or ts <= cutoff or not str(uid).lstrip("-").isdigit():
            continue
        seen.setdefault(f"{bot_id}:{uid}", float(ts))
    _trim(seen)


def _trim(seen: dict) -> None:
    """Keep the newest _SEEN_CAP admitted entries."""
    if len(seen) <= _SEEN_CAP:
        return
    newest = sorted(seen.items(), key=lambda item: item[1])[-_SEEN_CAP:]
    seen.clear()
    seen.update(newest)


def _persist(adapter, bot_id) -> None:
    """Write this bot's unexpired admitted IDs to its receipt file."""
    prefix, cutoff = f"{bot_id}:", time.time() - RECEIPT_TTL_SECONDS
    ids = {key[len(prefix):]: ts for key, ts in adapter._seen_update_ids.items()
           if key.startswith(prefix) and ts > cutoff}
    receipt_dir = adapter._update_receipt_dir
    path = _receipt_path(receipt_dir, bot_id)
    try:
        receipt_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, {"update_ids": ids})
    except Exception:
        logger.warning("[Telegram] Failed to persist update receipts to %s", path, exc_info=True)


def _expired(seen: dict, key: str, now: float) -> bool:
    """Drop ``key`` from ``seen`` when its entry is past the TTL; True if present and live."""
    seen_at = seen.get(key)
    if seen_at is not None and now - seen_at >= RECEIPT_TTL_SECONDS:
        # Expired (same rule as MessageDeduplicator): Telegram never
        # redelivers past 24h, and may recycle IDs after a week idle.
        del seen[key]
        return False
    return seen_at is not None


def _complete(adapter, bot_id, key: str) -> None:
    """Turn an in-flight claim into a persisted receipt (idempotent)."""
    inflight = adapter._inflight_update_ids
    if inflight.pop(key, None) is None:
        return
    adapter._inflight_with_event.discard(key)
    adapter._seen_update_ids[key] = time.time()
    _trim(adapter._seen_update_ids)
    _persist(adapter, bot_id)


def attach_receipt(adapter, bot_id, update_id, event) -> None:
    """Defer the receipt for ``update_id`` until ``event`` is durably handed off.

    The receipt is written when the gateway calls ``complete_inbound_handoff``
    on the event (turn marker set, recorded in the restart inbox, or the turn
    finished). Until then only the in-memory claim exists, so a crash lets
    Telegram's unacknowledged replay through.
    """
    if bot_id is None or update_id is None:
        return
    key = f"{bot_id}:{update_id}"
    if key not in adapter._inflight_update_ids:
        return
    adapter._inflight_with_event.add(key)
    done = []

    def _receipt() -> None:
        if not done:
            done.append(True)
            _complete(adapter, bot_id, key)

    receipts = getattr(event, "inbound_receipts", None)
    if receipts is not None:
        receipts.append(_receipt)


def make_admission_handler(adapter, bot_id):
    """Return the group -1 TypeHandler callback that claims each update once.

    ``adapter`` owns ``_seen_update_ids`` (completed receipts, persisted),
    ``_inflight_update_ids`` (claimed, in memory only) and ``_update_receipt_dir``.
    """

    async def admit(update, context) -> None:
        key = f"{bot_id}:{update.update_id}"
        now = time.time()
        if _expired(adapter._seen_update_ids, key, now):
            raise ApplicationHandlerStop  # already processed
        inflight = adapter._inflight_update_ids
        if _expired(inflight, key, now):
            raise ApplicationHandlerStop  # same-process replay while in flight
        # Claim synchronously (no await between the check and the write), so
        # two copies of the same update can never both pass. The claim is NOT
        # persisted: nothing durable exists for this update yet.
        inflight[key] = now
        _trim(inflight)

    return admit


def make_finalize_handler(adapter, bot_id):
    """Group-after-everything callback: complete updates that produced no event.

    An update whose handlers built no ``MessageEvent`` (callback query, ignored
    or unauthorized message, ...) has nothing to hand off, so it is complete.
    One that did build an event is completed by that event's handoff instead.
    """

    async def finalize(update, context) -> None:
        key = f"{bot_id}:{update.update_id}"
        if key in adapter._inflight_update_ids and key not in adapter._inflight_with_event:
            _complete(adapter, bot_id, key)

    return finalize

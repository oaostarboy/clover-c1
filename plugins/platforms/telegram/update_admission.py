"""Telegram update-ID admission ahead of every PTB handler group.

Telegram redelivers an update whose acknowledgement (the next getUpdates offset,
or the cleanup call in Updater.stop) never landed. That happens after a crash,
a reconnect, or an adapter rebuild, where the replacement adapter starts with an
empty in-memory set and a new PTB Updater polls from offset 0.

This gate runs in PTB group -1, before any core or plugin handler. An update ID
that was already admitted is answered once: a replay raises ApplicationHandlerStop
so no later group sees it. Admitted IDs are persisted to a per-bot receipt file
under CLOVER_HOME, so a rebuilt adapter or a restarted gateway still drops them.

Adapted from NousResearch/hermes-agent plugins/platforms/telegram/update_admission.py
(MIT), commits 992f569fc1 and a1838ea87a. The Hermes version subclasses PTB's
Application to pin dispatch claims and flushes receipts from a background task.
This port uses a plain group -1 handler with PTB's own ApplicationHandlerStop, and
writes each receipt synchronously before the update continues, so a crash right
after admission cannot lose the record. The receipt-file layout and 24h TTL are
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


def make_admission_handler(adapter, bot_id):
    """Return the group -1 TypeHandler callback that admits each update once.

    ``adapter`` owns ``_seen_update_ids`` (admitted IDs) and ``_update_receipt_dir``.
    """

    async def admit(update, context) -> None:
        key = f"{bot_id}:{update.update_id}"
        seen = adapter._seen_update_ids
        now = time.time()
        seen_at = seen.get(key)
        if seen_at is not None and now - seen_at >= RECEIPT_TTL_SECONDS:
            # Expired (same rule as MessageDeduplicator): Telegram never
            # redelivers past 24h, and may recycle IDs after a week idle.
            del seen[key]
            seen_at = None
        if seen_at is not None:
            # Replay of an already-admitted update: answer it once.
            raise ApplicationHandlerStop
        # Admit synchronously (no await between the check and the write), so two
        # copies of the same update can never both pass.
        seen[key] = now
        _trim(seen)
        _persist(adapter, bot_id)

    return admit

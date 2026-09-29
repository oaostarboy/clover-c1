"""Cold-boot pending-queue knob for the Telegram adapter.

Contract: a cold boot preserves Telegram's server-side pending updates unless
``extra.drop_pending_on_cold_boot`` is true; a watcher reconnect always
preserves them. Clover's default is the opposite of Hermes's: friends message
bots while Ant's box reboots or updates, so losing that backlog is worse than
a duplicate. Ported from Hermes's tests/gateway/test_telegram_cold_boot_queue.py,
adapted to Clover's inlined ``connect()`` (no separate ``_start_polling_mode``/
``_start_webhook_mode`` helpers) via the extracted ``_cold_boot_drop_pending``.
"""

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter(extra=None) -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra=extra or {}))


def test_cold_boot_preserves_queue_by_default():
    """Clover's default: cold boot preserves, reconnect preserves."""
    adapter = _make_adapter()
    assert adapter._drop_pending_on_cold_boot is False

    assert adapter._cold_boot_drop_pending(is_reconnect=False) is False
    assert adapter._cold_boot_drop_pending(is_reconnect=True) is False


def test_cold_boot_drops_queue_when_opted_in():
    """extra.drop_pending_on_cold_boot=true: cold boot drops, reconnect still preserves."""
    adapter = _make_adapter(extra={"drop_pending_on_cold_boot": True})
    assert adapter._drop_pending_on_cold_boot is True

    assert adapter._cold_boot_drop_pending(is_reconnect=False) is True
    assert adapter._cold_boot_drop_pending(is_reconnect=True) is False


def test_reconnect_always_preserves_regardless_of_knob():
    """A watcher reconnect keeps #46621's guarantee even with the knob flipped on."""
    adapter = _make_adapter(extra={"drop_pending_on_cold_boot": True})

    assert adapter._cold_boot_drop_pending(is_reconnect=True) is False

"""Regression tests for the Telegram polling error_callback log sites.

``c3ab1424e`` added ``_redact_telegram_error_text()`` and applied it across
the send/edit transient-error paths. The two log sites inside
``connect()``'s ``_polling_error_callback`` were converted incorrectly: the
helper *call* was pasted into the format-string literal instead of wrapping
the argument, so the emitted line read

    [Telegram] Telegram network _redact_telegram_error_text(error), scheduling
    reconnect: <raw exception>

— the literal helper name leaked into operator-facing logs AND the raw
exception was still interpolated unredacted. A Telegram transport error
routinely embeds the request URL
(``https://api.telegram.org/bot<TOKEN>/getUpdates``), so the live bot token
could reach the journal on every network blip or unclassified polling error.

These tests drive the *real* callback captured from ``start_polling`` rather
than asserting on source text, so they fail against the pre-fix code and stay
honest if the callback is refactored.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter

# Synthetic bot token, assembled at runtime so tooling that redacts
# token-shaped literals in source can't mangle it into a sentinel that
# would make these assertions vacuously pass. Shape matches a real
# Telegram token: <9 digits>:<35 chars>.
_SECRET_TOKEN = "123456789" + ":" + ("Zz" * 17) + "Q"
_SECRET_URL = f"https://api.telegram.org/bot{_SECRET_TOKEN}/getUpdates"


@pytest.fixture(autouse=True)
def _no_auto_discovery(monkeypatch):
    """Disable DoH auto-discovery so connect() uses the plain builder chain."""

    async def _noop():
        return []

    monkeypatch.setattr("plugins.platforms.telegram.adapter.discover_fallback_ips", _noop)
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.HTTPXRequest", lambda **kwargs: MagicMock()
    )


async def _cancel_heartbeat(adapter):
    """Cancel the lifetime heartbeat task connect() starts in polling mode."""
    task = getattr(adapter, "_polling_heartbeat_task", None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    adapter._polling_heartbeat_task = None


async def _connect_capturing_error_callback(monkeypatch):
    """Run the real connect() and hand back (adapter, error_callback)."""
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token=_SECRET_TOKEN))

    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None),
    )
    monkeypatch.setattr("gateway.status.release_scoped_lock", lambda scope, identity: None)

    captured = {}

    async def fake_start_polling(**kwargs):
        captured["error_callback"] = kwargs["error_callback"]
        adapter._record_polling_progress(adapter._polling_generation)

    updater = SimpleNamespace(
        start_polling=AsyncMock(side_effect=fake_start_polling),
        stop=AsyncMock(),
        running=True,
    )
    bot = SimpleNamespace(set_my_commands=AsyncMock(), delete_webhook=AsyncMock())
    app = SimpleNamespace(
        bot=bot,
        updater=updater,
        add_handler=MagicMock(),
        initialize=AsyncMock(),
        start=AsyncMock(),
    )
    builder = MagicMock()
    builder.token.return_value = builder
    builder.request.return_value = builder
    builder.get_updates_request.return_value = builder
    builder.build.return_value = app
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.Application",
        SimpleNamespace(builder=MagicMock(return_value=builder)),
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    assert await adapter.connect() is True
    assert callable(captured["error_callback"])
    return adapter, captured["error_callback"]


async def _drain_polling_error_task(adapter):
    task = adapter._polling_error_task
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    adapter._polling_error_task = None


@pytest.mark.asyncio
async def test_polling_network_error_log_redacts_token(monkeypatch, caplog):
    """A network-classified polling error must be logged redacted, and the
    log line must not contain the redaction helper's own name."""
    adapter, error_callback = await _connect_capturing_error_callback(monkeypatch)

    from telegram.error import NetworkError

    try:
        with caplog.at_level("WARNING"):
            error_callback(NetworkError(f"Bad Gateway from {_SECRET_URL}"))

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert _SECRET_TOKEN not in logged, "bot token leaked into the reconnect warning"
        assert "_redact_telegram_error_text(" not in logged, (
            "helper name leaked into the log message literal"
        )
        assert "scheduling reconnect" in logged
    finally:
        await _drain_polling_error_task(adapter)
        await _cancel_heartbeat(adapter)


@pytest.mark.asyncio
async def test_unclassified_polling_error_log_redacts_token(monkeypatch, caplog):
    """An error that is neither a conflict nor a network error takes the
    ``logger.error`` branch — it must be redacted the same way."""
    adapter, error_callback = await _connect_capturing_error_callback(monkeypatch)

    try:
        with caplog.at_level("ERROR"):
            error_callback(Exception(f"totally unexpected failure at {_SECRET_URL}"))

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert _SECRET_TOKEN not in logged, "bot token leaked into the polling error log"
        assert "_redact_telegram_error_text(" not in logged, (
            "helper name leaked into the log message literal"
        )
        assert "Telegram polling error" in logged
    finally:
        await _drain_polling_error_task(adapter)
        await _cancel_heartbeat(adapter)

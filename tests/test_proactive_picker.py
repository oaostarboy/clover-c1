"""Production Telegram picker send + callback route, with offline transport only."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.workstreams import load_settings
from clover_constants import set_clover_home_override, reset_clover_home_override
from gateway.config import Platform
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_proactive_command import runner, event, home


@pytest.fixture
def picker(home):
    r = runner()
    a = TelegramAdapter(r.config.platforms[Platform.TELEGRAM])
    a._bot = MagicMock()
    a._send_message_with_thread_fallback = AsyncMock(return_value=SimpleNamespace(message_id=41))
    a._is_callback_user_authorized = lambda *args, **kwargs: True
    r.adapters = {Platform.TELEGRAM: a}
    return r, a


def query(*, uid="owner", chat="c1", msg=41, data="cp:0", thread=None):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid, first_name="Owner"),
        message=SimpleNamespace(chat_id=chat, message_id=msg, message_thread_id=thread,
                                chat=SimpleNamespace(type="private")),
        answer=AsyncMock(), edit_message_text=AsyncMock())


async def tap(adapter, q):
    await adapter._handle_callback_query(SimpleNamespace(callback_query=q), None)


@pytest.mark.asyncio
async def test_native_enable_single_use_no_model_and_profile_pinned(picker, home, tmp_path):
    r, a = picker
    assert await r._handle_message(event("/proactive")) is None
    wire = a._send_message_with_thread_fallback.call_args.kwargs
    buttons = wire["reply_markup"].inline_keyboard[0]
    assert [b.text.lstrip("✓") for b in buttons] == ["Enable", "Disable"]
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    other = tmp_path / "other-profile"
    other.mkdir()
    token = set_clover_home_override(str(other))
    try:
        q = query(data=buttons[0].callback_data)
        await tap(a, q)
        assert not load_settings().enabled  # The callback must NOT write this ambient profile.
        assert not (other / "config.yaml").exists()
        assert "Saved" in str(q.edit_message_text.call_args)
        await tap(a, q)  # Replay cannot reapply or select the other choice.
    finally:
        reset_clover_home_override(token)
    assert load_settings().active
    assert not a._choice_picker_state


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["user", "message", "chat", "thread", "expired", "revoked", "profile", "negative-index", "transport-denied"])
async def test_unowned_or_stale_callbacks_cannot_toggle(picker, home, tmp_path, bad):
    r, a = picker
    assert await r._handle_message(event("/proactive")) is None
    assert "c1" in a._choice_picker_state
    q = query()
    if bad == "user": q.from_user.id = "guest"
    if bad == "message": q.message.message_id = 40
    if bad == "chat": q.message.chat_id = "foreign"
    if bad == "thread": q.message.message_thread_id = 9
    if bad == "expired": a._choice_picker_state["c1"]["guard"]["expires_at"] = time.monotonic() - 1
    if bad == "revoked": r.config.platforms[Platform.TELEGRAM].extra["allow_admin_from"] = ["new-owner"]
    if bad == "profile": r._resolve_profile_home_for_source = lambda source: tmp_path / "foreign"
    if bad == "negative-index": q.data = "cp:-1"
    if bad == "transport-denied": a._is_callback_user_authorized = lambda *args, **kwargs: False
    await tap(a, q)
    assert not load_settings().enabled
    assert not (home / "config.yaml").exists()
    q.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_send_failure_falls_back_to_accessible_text(picker, home):
    r, a = picker
    a._send_message_with_thread_fallback.side_effect = RuntimeError("offline")
    text = await r._handle_message(event("/proactive"))
    assert "Off" in text and "/proactive on" in text
    assert not (home / "config.yaml").exists()


@pytest.mark.asyncio
async def test_finishing_old_tap_does_not_remove_new_menu(picker, home):
    r, a = picker
    assert await r._handle_message(event("/proactive")) is None
    q = query()

    async def open_new_menu(**kwargs):
        a._send_message_with_thread_fallback.return_value = SimpleNamespace(message_id=42)
        assert await r._handle_message(event("/proactive")) is None

    q.edit_message_text.side_effect = open_new_menu
    await tap(a, q)
    assert a._choice_picker_state["c1"]["msg_id"] == 42


@pytest.mark.asyncio
async def test_callback_persistence_failure_never_claims_saved(picker, home, monkeypatch):
    from clover_cli import config
    r, a = picker
    assert await r._handle_message(event("/proactive")) is None
    monkeypatch.setattr(config, "save_config", lambda *a, **k: None)
    q = query()
    await tap(a, q)
    assert "could not be confirmed" in str(q.edit_message_text.call_args)
    assert "Saved" not in str(q.edit_message_text.call_args)
    assert not load_settings().enabled

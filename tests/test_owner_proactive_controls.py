"""Octavia-shaped no-admin policy through real dispatch and native PTB markup."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardMarkup  # Import genuine PTB before gateway fixtures.

from agent.workstreams import load_settings
from clover_cli import config
from clover_constants import set_clover_home_override, reset_clover_home_override
from gateway.config import Platform, load_gateway_config
from gateway.pairing import VerifiedOwnerStore
from gateway.run import GatewayRunner
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_proactive_command import home, event
from tests.gateway.test_slash_access_dispatch import _make_runner
from tests.test_proactive_picker import query, tap

TOKEN = "123456:test-token"
UID = "12345678"


def transport(home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    config.save_config({"platforms": {"telegram": {"enabled": True, "allow_from": [UID, "87654321"]}},
                        "proactive": {"enabled": False, "mode": "active"}}, strip_defaults=False)
    config.load_config()
    r = _make_runner(platform=Platform.TELEGRAM)
    r.config = load_gateway_config()
    r._is_user_authorized = GatewayRunner._is_user_authorized.__get__(r)
    a = TelegramAdapter(r.config.platforms[Platform.TELEGRAM])
    a._bot = MagicMock()
    a._send_message_with_thread_fallback = AsyncMock(return_value=SimpleNamespace(message_id=41))
    a._message_handler = r._handle_message
    r.adapters = {Platform.TELEGRAM: a}
    r._handle_message_with_agent = AsyncMock(side_effect=AssertionError("no model"))
    return r, a


def verify(home):
    s = VerifiedOwnerStore(TOKEN, home=home)
    challenge = s.begin()
    code = s.prove(challenge, UID)
    assert s.confirm(code) == UID
    return s


@pytest.mark.asyncio
async def test_verified_owner_without_admin_lists_has_buttons_guests_unchanged_and_off_preserved(home, monkeypatch):
    r, a = transport(home, monkeypatch)
    src = event("/proactive", UID).source
    guest = event("/proactive", "87654321").source
    assert r._proactive_caller_is_admin(src)
    assert r._check_slash_access(guest, "model") is None
    before = (home / "config.yaml").read_bytes()
    verify(home)
    assert r._proactive_caller_is_admin(src), "verified local + Telegram ownership must unlock profile controls"
    assert r._proactive_caller_is_admin(guest)  # Unrestricted control policy, not an owner grant.
    assert r._check_slash_access(guest, "model") is None
    assert await r._handle_message(event("/proactive", UID)) is None
    wire = a._send_message_with_thread_fallback.call_args.kwargs
    assert isinstance(wire["reply_markup"], InlineKeyboardMarkup)
    assert [b.text for b in wire["reply_markup"].inline_keyboard[0]] == ["On", "Off"]
    print("OWNER_NATIVE_MENU=" + json.dumps({"text": wire["text"], "markup": wire["reply_markup"].to_dict()}))
    assert wire["text"].startswith("Proactive: Off") and len(wire["text"]) < 280
    assert (home / "config.yaml").read_bytes() == before
    assert not load_settings().enabled
    q = query(uid=UID, data=wire["reply_markup"].inline_keyboard[0][0].callback_data)
    await tap(a, q)
    assert load_settings().enabled
    assert "Saved" in str(q.edit_message_text.call_args)
    r._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["revoked-admission", "explicit-admin-denial", "group", "missing-user"])
async def test_previously_valid_owner_is_denied_after_one_scope_or_policy_change(home, monkeypatch, tmp_path, bad):
    r, a = transport(home, monkeypatch)
    verify(home)
    a.config.extra["group_allow_admin_from"] = ["87654321"]
    src = event("/proactive", UID).source
    assert r._proactive_caller_is_admin(src)
    assert await r._handle_message(event("/proactive", UID)) is None
    before = (home / "config.yaml").read_bytes()
    if bad == "revoked-admission":
        a.config.extra["allow_from"] = ["87654321"]
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "87654321")
    if bad == "explicit-admin-denial": a.config.extra["allow_admin_from"] = ["87654321"]
    if bad == "group": src.chat_type = "group"
    if bad == "missing-user": src.user_id = None
    assert not r._proactive_caller_is_admin(src)
    # For source-shape changes the transport's independent actor/thread/chat
    # guard has coverage in the shipped picker suite; here mutate authority.
    if bad not in {"group", "missing-user", "other-user"}:
        q = query(uid=UID)
        await tap(a, q)
        q.edit_message_text.assert_not_called()
    assert not load_settings().enabled
    assert (home / "config.yaml").read_bytes() == before


@pytest.mark.asyncio
async def test_no_proof_permission_help_is_readonly_and_not_an_enrollment(home, monkeypatch):
    r, a = transport(home, monkeypatch)
    before = (home / "config.yaml").read_bytes()
    for arg in ("setup", "status", "setup 12345678", "setup @owner"):
        text = await r._handle_message(event("/proactive " + arg, UID))
        assert isinstance(text, str)
    text = await r._handle_message(event("/proactive setup", UID))
    assert "No separate owner verification" in text
    assert "command policy" in text and "allow_admin_from" in text
    assert r._proactive_caller_is_admin(event("/proactive", UID).source)
    assert (home / "config.yaml").read_bytes() == before
    assert not load_settings().enabled
    assert not VerifiedOwnerStore(TOKEN, home=home).path.exists()
    r._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
async def test_saved_verified_owner_recovers_with_new_runtime_and_can_disable(home, monkeypatch):
    r, a = transport(home, monkeypatch)
    verify(home)
    r2 = _make_runner(platform=Platform.TELEGRAM)
    r2.config = load_gateway_config()
    r2._is_user_authorized = GatewayRunner._is_user_authorized.__get__(r2)
    a2 = TelegramAdapter(r2.config.platforms[Platform.TELEGRAM])
    a2._bot = MagicMock()
    a2._send_message_with_thread_fallback = AsyncMock(return_value=SimpleNamespace(message_id=41))
    a2._message_handler = r2._handle_message
    r2.adapters = {Platform.TELEGRAM: a2}
    assert r2._proactive_caller_is_admin(event("/proactive", UID).source)
    assert await r2._handle_message(event("/proactive", UID)) is None
    await tap(a2, query(uid=UID, data="cp:1"))
    assert not load_settings().enabled


@pytest.mark.asyncio
async def test_managed_fallback_switch_is_not_a_command_control_denial(home, monkeypatch, tmp_path):
    from clover_cli import managed_scope
    r, a = transport(home, monkeypatch)
    verify(home)
    assert r._proactive_caller_is_admin(event("/proactive", UID).source)
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("platforms:\n  telegram:\n    verified_owner_controls: false\n")
    monkeypatch.setenv("CLOVER_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        r.config = load_gateway_config()
        a.config = r.config.platforms[Platform.TELEGRAM]
        assert r._proactive_caller_is_admin(event("/proactive", UID).source)
        assert r._check_slash_access(event("/model", "87654321").source, "model") is None
    finally:
        managed_scope.invalidate_managed_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("managed_yaml", [
    "platforms:\n  telegram:\n    verified_owner_controls: false\n",
    "platforms:\n  telegram:\n    extra:\n      verified_owner_controls: false\n",
    "platforms:\n  telegram:\n    allow_admin_from: ['87654321']\n",
])
async def test_current_managed_admin_denial_not_fallback_switch_limits_command_authority(home, monkeypatch, tmp_path, managed_yaml):
    from clover_cli import managed_scope
    r, a = transport(home, monkeypatch)
    verify(home)
    config.set_config_value("platforms.telegram.verified_owner_controls", "true")
    assert r._proactive_caller_is_admin(event("/proactive", UID).source)
    managed = tmp_path / "managed-current"
    managed.mkdir()
    (managed / "config.yaml").write_text(managed_yaml)
    monkeypatch.setenv("CLOVER_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        assert r._proactive_caller_is_admin(event("/proactive", UID).source) is ("allow_admin_from" not in managed_yaml)
    finally:
        managed_scope.invalidate_managed_cache()

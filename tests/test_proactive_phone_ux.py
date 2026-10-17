"""Phone-sized menu and explicit enrollment, through real dispatch/transport."""
import json
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest

from clover_cli import config
from gateway.config import Platform, load_gateway_config
from gateway.run import GatewayRunner
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_proactive_command import home, event
from tests.gateway.test_slash_access_dispatch import _make_runner


@pytest.mark.asyncio
async def test_unenrolled_live_shape_is_short_and_explains_missing_controls(home):
    # The screenshot's relevant state: active settings, no admin enrollment.
    config.save_config({"proactive": {"enabled": True, "mode": "active"}}, strip_defaults=False)
    config.load_config()  # Establish current-schema baseline before gateway admission.
    r = _make_runner(platform=Platform.TELEGRAM)
    a = TelegramAdapter(r.config.platforms[Platform.TELEGRAM])
    a._bot = MagicMock()
    a._send_message_with_thread_fallback = AsyncMock()
    r.adapters = {Platform.TELEGRAM: a}
    before = (home / "config.yaml").read_bytes()
    text = await r._handle_message(event("/proactive"))
    assert isinstance(text, str)
    print("PROACTIVE_UX_UNENROLLED=" + json.dumps({"text": text, "characters": len(text), "reply_markup": None}))
    assert len(text) <= 260
    assert text.startswith("Proactive: On\n")
    assert "admin" in text.lower() and "/whoami" in text
    assert "/proactive status" in text
    assert "/proactive on" not in text
    a._send_message_with_thread_fallback.assert_not_called()
    assert (home / "config.yaml").read_bytes() == before


def enrolled_transport(home, *, enabled=True):
    # Existing CLI configuration owner, not an invented chat/DM owner inference.
    config.save_config({"platforms": {"telegram": {"enabled": True, "allow_from": ["12345678"]}},
                        "proactive": {"enabled": enabled, "mode": "active"}}, strip_defaults=False)
    config.set_config_value("platforms.telegram.allow_admin_from", '["12345678"]')
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


@pytest.mark.asyncio
@pytest.mark.parametrize("value,initial,index", [("on", False, 0), ("off", True, 1)])
async def test_cli_enrolled_owner_gets_short_native_menu_and_compact_callback(home, value, initial, index):
    from tests.test_proactive_picker import query, tap
    from agent.workstreams import load_settings
    r, a = enrolled_transport(home, enabled=initial)
    assert r._proactive_caller_is_admin(event("/proactive", "12345678").source)
    assert await r._handle_message(event("/proactive", "12345678")) is None
    wire = a._send_message_with_thread_fallback.call_args.kwargs
    markup = wire["reply_markup"].to_dict()
    print("PROACTIVE_UX_ENROLLED=" + json.dumps({"text": wire["text"], "characters": len(wire["text"]), "reply_markup": markup}))
    assert len(wire["text"]) <= 160
    assert "/proactive on" not in wire["text"]
    buttons = wire["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == ["Enable", "Disable"]
    q = query(uid="12345678", data=buttons[index].callback_data)
    await tap(a, q)
    result = q.edit_message_text.call_args.kwargs["text"]
    print("PROACTIVE_UX_CALLBACK=" + json.dumps({"action": value, "text": result, "characters": len(result), "reply_markup": q.edit_message_text.call_args.kwargs["reply_markup"]}))
    assert len(result) <= 180
    assert f"Proactive: {value.title()}" in result and "Saved" in result and "next turn" in result
    assert load_settings().enabled is (value == "on")
    r._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type,key", [("dm", "allow_admin_from"), ("group", "group_allow_admin_from")])
async def test_status_has_scoped_local_setup_with_policy_side_effect_warning(home, chat_type, key):
    r = _make_runner(platform=Platform.TELEGRAM)
    text = await r._handle_message(event("/proactive status", "12345678", chat_type=chat_type))
    assert isinstance(text, str)
    if chat_type == "dm":
        assert "clover pairing owner-setup telegram" in text
        assert "confirmation code" in text and "Guest access" in text
        assert "no policy reload" in text
        assert "clover config set" not in text  # Enrollment must not silently gate guest commands.
    else:
        assert f"clover config set platforms.telegram.{key}" in text
        assert "other users" in text and "slash commands" in text
        assert "reload" in text.lower()
    assert "/whoami" in text
    assert "current profile" in text
    assert "--force" not in text and "12345678" not in text
    assert not (home / "config.yaml").exists()


def test_tui_dispatch_uses_same_short_controls_without_a_model(home):
    from tui_gateway import server
    from agent.workstreams import load_settings

    def dispatch(arg):
        response = server.handle_request({"id": "proactive-ux", "method": "command.dispatch",
                                          "params": {"name": "proactive", "arg": arg}})
        assert "error" not in response
        assert response["result"]["type"] == "exec"  # Not a model/send action.
        return response["result"]["output"]

    text = dispatch("")
    assert text.startswith("Proactive: Off\n") and len(text) <= 200
    assert "/proactive on" in text and "/proactive off" in text
    assert not (home / "config.yaml").exists()
    assert "Saved" in dispatch("on") and load_settings().active
    assert "Saved" in dispatch("off") and not load_settings().enabled
    assert "Effective settings:" in dispatch("status")


@pytest.mark.asyncio
async def test_readonly_text_fallback_uses_adapter_discovery_prefix(home):
    class TypedAdapter:
        typed_command_prefix = "!"
    r = _make_runner(platform=Platform.TELEGRAM)
    r.adapters = {Platform.TELEGRAM: TypedAdapter()}
    text = await r._handle_message(event("/proactive"))
    assert isinstance(text, str)
    assert "!whoami" in text and "!proactive status" in text
    assert "/whoami" not in text and "/proactive" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["single-allowlist", "wildcard", "open"])
async def test_chat_admission_never_bootstraps_admin(home, monkeypatch, access):
    r = _make_runner(platform=Platform.TELEGRAM)
    r._is_user_authorized = GatewayRunner._is_user_authorized.__get__(r)
    extra = r.config.platforms[Platform.TELEGRAM].extra
    extra["allow_from"] = ["*"] if access == "wildcard" else ["12345678"]
    if access == "open":
        extra.clear()
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    a = TelegramAdapter(r.config.platforms[Platform.TELEGRAM])
    r.adapters = {Platform.TELEGRAM: a}
    src = event("/proactive on", "12345678").source
    assert r._is_user_authorized_for_source(src)  # Eligible admission baseline.
    assert not r._proactive_caller_is_admin(src)
    text = await r._handle_message(event("/proactive on", "12345678"))
    assert isinstance(text, str) and "admin" in text and len(text) < 180
    assert not (home / "config.yaml").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["chat-id-as-user", "missing-user", "other-user", "revoked-admission", "revoked-admin"])
async def test_real_enrollment_cannot_be_substituted_or_revoked_at_tap(home, monkeypatch, bad):
    from tests.test_proactive_picker import query, tap
    r, a = enrolled_transport(home)
    src = event("/proactive", "12345678").source
    assert r._proactive_caller_is_admin(src)  # Positive before a single mutation.
    assert await r._handle_message(event("/proactive", "12345678")) is None
    before = (home / "config.yaml").read_bytes()
    q = query(uid="12345678", data="cp:1")
    if bad == "chat-id-as-user":
        q.from_user.id = "c1"
    elif bad == "missing-user":
        q.from_user.id = ""
    elif bad == "other-user":
        q.from_user.id = "87654321"
    elif bad == "revoked-admission":
        # The captured runtime scope derives gates from the profile file as
        # well as the adapter; revoke the real source, not just one snapshot.
        raw = config.require_readable_config_before_write()
        raw["platforms"]["telegram"]["allow_from"] = ["87654321"]
        config.save_config(raw, strip_defaults=False, strip_managed=False)
        a.config.extra["allow_from"] = ["87654321"]
        # The adapter's config bridge also populated this process-level gate.
        # Model an applied policy reload, not an un-reloaded file-only edit.
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "87654321")
        before = (home / "config.yaml").read_bytes()
        assert not r._is_user_authorized_for_source(src)
    elif bad == "revoked-admin":
        r.config.platforms[Platform.TELEGRAM].extra["allow_admin_from"] = ["87654321"]
    await tap(a, q)
    q.edit_message_text.assert_not_called()
    assert (home / "config.yaml").read_bytes() == before


@pytest.mark.asyncio
async def test_simultaneous_owner_taps_only_write_once(home, monkeypatch):
    import asyncio
    from tests.test_proactive_picker import query, tap
    from agent.workstreams import load_settings
    r, a = enrolled_transport(home)
    assert await r._handle_message(event("/proactive", "12345678")) is None
    writes = []
    save = config.save_config

    def record_save(*args, **kwargs):
        writes.append(args[0])
        return save(*args, **kwargs)

    monkeypatch.setattr(config, "save_config", record_save)
    q1, q2 = query(uid="12345678", data="cp:1"), query(uid="12345678", data="cp:0")
    await asyncio.gather(tap(a, q1), tap(a, q2))
    assert len(writes) == 1 and not load_settings().enabled
    assert q1.edit_message_text.await_count == 1
    q2.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_compact_callback_reports_saved_effective_divergence(home, tmp_path, monkeypatch):
    from tests.test_proactive_picker import query, tap
    from clover_cli import managed_scope
    r, a = enrolled_transport(home)
    raw = config.require_readable_config_before_write()
    raw["proactive"]["enabled"] = False
    config.save_config(raw, strip_defaults=False, strip_managed=False)
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("proactive:\n  enabled: true\n  mode: active\n")
    monkeypatch.setenv("CLOVER_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        before = (home / "config.yaml").read_bytes()
        assert await r._handle_message(event("/proactive", "12345678")) is None
        q = query(uid="12345678", data="cp:1")
        await tap(a, q)
        result = q.edit_message_text.call_args.kwargs["text"]
        assert "Proactive: On" in result
        assert "Saved: Off" in result and "effective: On" in result
        assert "managed policy" in result and "no change" in result
        assert "applies next turn" not in result and len(result) <= 240
        assert (home / "config.yaml").read_bytes() == before
    finally:
        managed_scope.invalidate_managed_cache()

"""Real PTB command, keyboard HTTP serialization and callback authorization.

Only Telegram's network endpoint is replaced. No owner record or admin list is
needed when the existing scoped command policy deliberately permits control.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import Application

from agent.workstreams import load_settings
from gateway.config import Platform
from gateway.pairing import VerifiedOwnerStore
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.test_owner_proactive_controls import TOKEN, UID, transport
from tests.test_owner_telegram_wire import TelegramEndpoint, update
from tests.gateway.test_proactive_command import home  # noqa: F401


def command(app, text, **kwargs):
    data = update(app, text, **kwargs).to_dict()
    data["message"]["entities"][0]["length"] = len(text.split()[0])
    return Update.de_json(data, app.bot)


def callback(app, data, *, uid=UID, chat=None, msg=50, thread=None, kind="private"):
    message = {"message_id": msg, "date": 1800000000,
               "chat": {"id": int(chat or UID), "type": kind},
               "text": "Proactive: Off"}
    if thread is not None:
        message["message_thread_id"] = thread
    return Update.de_json({"update_id": 2, "callback_query": {
        "id": "tap-on", "chat_instance": "one", "data": data,
        "from": {"id": int(uid), "is_bot": False, "first_name": "User"},
        "message": message,
    }}, app.bot)


def wire(home, monkeypatch):
    r, a = transport(home, monkeypatch)
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a._bot = app.bot
    a._send_message_with_thread_fallback = TelegramAdapter._send_message_with_thread_fallback.__get__(a)
    a._register_handlers(app)
    errors = AsyncMock()
    app.add_error_handler(errors)
    return r, a, app, endpoint, errors


async def dispatch(app, adapter, raw):
    await app.process_update(raw)
    if adapter._background_tasks:
        await asyncio.wait_for(asyncio.gather(*list(adapter._background_tasks)), timeout=10)


@pytest.mark.asyncio
async def test_admitted_no_admin_no_owner_menu_on_readback_and_off(home, monkeypatch):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    owner_path = VerifiedOwnerStore(TOKEN, home=home).path
    assert not owner_path.exists()
    before = (home / "config.yaml").read_bytes()
    async with app:
        await dispatch(app, a, command(app, "/proactive"))
        errors.assert_not_called()
        sends = [p for m, p in endpoint.calls if m == "sendMessage"]
        assert len(sends) == 1
        menu = sends[0]
        assert menu["text"].startswith("Proactive: Off\n")
        assert "reply_markup" in menu, "admitted command-authorized sender needs actual native controls"
        markup = menu["reply_markup"]
        assert [b["text"] for b in markup["inline_keyboard"][0]] == ["On", "Off"]
        json.dumps(markup)
        assert (home / "config.yaml").read_bytes() == before
        assert not load_settings().enabled
        await app.process_update(callback(app, markup["inline_keyboard"][0][0]["callback_data"]))
        assert load_settings().enabled
        edits = [p for m, p in endpoint.calls if m == "editMessageText"]
        assert len(edits) == 1 and "Saved" in edits[0]["text"]
        await dispatch(app, a, command(app, "/proactive status"))
        assert "Effective settings: On" in [p for m, p in endpoint.calls if m == "sendMessage"][-1]["text"]
        await dispatch(app, a, command(app, "/proactive"))
        menu2 = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
        await app.process_update(callback(app, menu2["reply_markup"]["inline_keyboard"][0][1]["callback_data"]))
        assert not load_settings().enabled
        print("COMMAND_ACCESS_REAL_PTB_WIRE=" + json.dumps({"menu": menu, "on": edits[0], "off": [p for m, p in endpoint.calls if m == "editMessageText"][-1]}))
    errors.assert_not_called()
    r._handle_message_with_agent.assert_not_called()
    assert not owner_path.exists(), "command access must never enroll or infer an owner"


@pytest.mark.asyncio
async def test_configured_guest_listing_cannot_write_and_gets_permission_help(home, monkeypatch):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    a.config.extra.update(allow_admin_from=["87654321"], user_allowed_commands=["proactive"])
    before = (home / "config.yaml").read_bytes()
    async with app:
        for text in ("/proactive", "/proactive on", "/proactive off", "/proactive setup", "/proactive status"):
            await dispatch(app, a, command(app, text))
        sends = [p for m, p in endpoint.calls if m == "sendMessage"]
        assert len(sends) == 5
        assert all("reply_markup" not in p for p in sends)
        assert "command policy" in sends[1]["text"].lower()
        assert all("enrollment required" not in p["text"] and "owner-setup" not in p["text"] for p in sends)
        assert "allow_admin_from" in sends[-1]["text"].replace("\\", "")
    assert (home / "config.yaml").read_bytes() == before
    assert not load_settings().enabled
    errors.assert_not_called()
    r._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("kind", ["private", "supergroup"])
async def test_current_managed_admin_denial_blocks_open_callback_and_text(home, monkeypatch, tmp_path, nested, kind):
    from clover_cli import managed_scope
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    a.config.extra.update(group_allow_from=[UID], require_mention=False)
    async with app:
        await dispatch(app, a, command(app, "/proactive", kind=kind))
        menu = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
        managed = tmp_path / "managed"
        managed.mkdir()
        body = {"allow_admin_from" if kind == "private" else "group_allow_admin_from": ["87654321"]}
        if nested:
            body = {"extra": body}
        (managed / "config.yaml").write_text(json.dumps({"platforms": {"telegram": body}}))
        monkeypatch.setenv("CLOVER_MANAGED_DIR", str(managed))
        managed_scope.invalidate_managed_cache()
        before = (home / "config.yaml").read_bytes()
        try:
            await app.process_update(callback(app, menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                                              kind=kind, chat=UID if kind == "private" else -10077))
            await dispatch(app, a, command(app, "/proactive on", kind=kind))
            assert not load_settings().enabled
            assert (home / "config.yaml").read_bytes() == before
            assert not [p for m, p in endpoint.calls if m == "editMessageText"]
        finally:
            managed_scope.invalidate_managed_cache()
    errors.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["other-user", "message", "chat", "thread", "expired", "revoked-admin", "revoked-source", "profile", "adapter", "credentials"])
async def test_open_command_authorized_picker_rechecks_binding_and_current_authority(home, monkeypatch, tmp_path, bad):
    import time
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    async with app:
        await dispatch(app, a, command(app, "/proactive"))
        menu = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
        data = menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        kwargs = {}
        if bad == "other-user": kwargs["uid"] = "87654321"
        if bad == "message": kwargs["msg"] = 49
        if bad == "chat": kwargs["chat"] = "87654321"
        if bad == "thread": kwargs["thread"] = 9
        if bad == "expired": a._choice_picker_state[UID]["guard"]["expires_at"] = time.monotonic() - 1
        if bad == "revoked-admin": a.config.extra["allow_admin_from"] = ["87654321"]
        if bad == "revoked-source":
            from clover_cli import config
            raw = config.require_readable_config_before_write()
            raw["platforms"]["telegram"]["allow_from"] = ["87654321"]
            config.save_config(raw, strip_defaults=False, strip_managed=False)
            a.config.extra["allow_from"] = ["87654321"]
            monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "87654321")
        if bad == "profile": r._resolve_profile_home_for_source = lambda source: tmp_path / "foreign"
        if bad == "adapter": r.adapters[Platform.TELEGRAM] = object()
        if bad == "credentials": a.config.token = "654321:other-bot"
        before = (home / "config.yaml").read_bytes()
        await app.process_update(callback(app, data, **kwargs))
        assert not load_settings().enabled
        assert (home / "config.yaml").read_bytes() == before
        assert not [p for m, p in endpoint.calls if m == "editMessageText"]
    errors.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action,enabled", [("on", True), ("off", False), ("enable", True), ("disable", False)])
async def test_no_owner_text_actions_preserve_unrelated_config(home, monkeypatch, action, enabled):
    from clover_cli import config
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    raw = config.require_readable_config_before_write()
    raw["display"] = {"skin": "unchanged"}
    raw["proactive"].update(enabled=not enabled, suggestions={"enabled": False}, eligible_jobs=["never-enrolled"])
    config.save_config(raw, strip_defaults=False, strip_managed=False)
    async with app:
        await dispatch(app, a, command(app, "/proactive " + action))
        assert load_settings().enabled is enabled
        saved = config.require_readable_config_before_write()
        assert saved == {**raw, "proactive": {**raw["proactive"], "enabled": enabled}}
        assert "Saved" in [p for m, p in endpoint.calls if m == "sendMessage"][-1]["text"]
    errors.assert_not_called()
    assert not VerifiedOwnerStore(TOKEN, home=home).path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_disabled_verified_owner_fallback_does_not_deny_command_authority(home, monkeypatch, tmp_path, managed):
    from clover_cli import managed_scope
    from gateway.slash_access import telegram_verified_owner_allowed
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    a.config.extra["verified_owner_controls"] = False
    if managed:
        directory = tmp_path / "managed-fallback-only"
        directory.mkdir()
        (directory / "config.yaml").write_text("platforms:\n  telegram:\n    verified_owner_controls: false\n")
        monkeypatch.setenv("CLOVER_MANAGED_DIR", str(directory))
    managed_scope.invalidate_managed_cache()
    try:
        assert not telegram_verified_owner_allowed(a.config, UID)
        async with app:
            await dispatch(app, a, command(app, "/proactive"))
            menu = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
            await app.process_update(callback(app, menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"]))
            assert load_settings().enabled
        errors.assert_not_called()
        assert not VerifiedOwnerStore(TOKEN, home=home).path.exists()
    finally:
        managed_scope.invalidate_managed_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_managed_feature_rejection_at_callback_and_text_is_not_success(home, monkeypatch, tmp_path, nested):
    from clover_cli import managed_scope
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    async with app:
        await dispatch(app, a, command(app, "/proactive"))
        menu = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
        directory = tmp_path / "managed-feature"
        directory.mkdir()
        body = {"enabled": False} if nested else False
        (directory / "config.yaml").write_text(json.dumps({"proactive": body}))
        monkeypatch.setenv("CLOVER_MANAGED_DIR", str(directory))
        managed_scope.invalidate_managed_cache()
        before = (home / "config.yaml").read_bytes()
        try:
            await app.process_update(callback(app, menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"]))
            await dispatch(app, a, command(app, "/proactive on"))
            replies = [p["text"] for m, p in endpoint.calls if m in {"sendMessage", "editMessageText"}][1:]
            assert len(replies) == 2
            assert all("could not be confirmed" in text and "Saved" not in text for text in replies)
            assert not load_settings().enabled
            assert (home / "config.yaml").read_bytes() == before
        finally:
            managed_scope.invalidate_managed_cache()
    errors.assert_not_called()


@pytest.mark.asyncio
async def test_replay_of_real_serialized_callback_does_not_overwrite_newer_text_choice(home, monkeypatch):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    async with app:
        await dispatch(app, a, command(app, "/proactive"))
        menu = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
        cb = callback(app, menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"])
        await app.process_update(cb)
        assert load_settings().enabled
        await dispatch(app, a, command(app, "/proactive off"))
        before = (home / "config.yaml").read_bytes()
        await app.process_update(cb)
        assert not load_settings().enabled
        assert (home / "config.yaml").read_bytes() == before
        assert len([p for m, p in endpoint.calls if m == "editMessageText"]) == 1
    errors.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("group_admin", [False, True])
async def test_dm_admin_does_not_override_separate_group_command_control_policy(home, monkeypatch, group_admin):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    a.config.extra.update(allow_admin_from=[UID], group_allow_from=[UID], require_mention=False,
                          group_allow_admin_from=[UID if group_admin else "87654321"],
                          group_user_allowed_commands=["proactive"])
    before = (home / "config.yaml").read_bytes()
    async with app:
        await dispatch(app, a, command(app, "/proactive", kind="supergroup"))
        menu = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
        assert ("reply_markup" in menu) is group_admin
        assert (home / "config.yaml").read_bytes() == before
        await dispatch(app, a, command(app, "/proactive on", kind="supergroup"))
        assert load_settings().enabled is group_admin
        if not group_admin:
            assert (home / "config.yaml").read_bytes() == before
    errors.assert_not_called()


@pytest.mark.asyncio
async def test_default_off_and_source_denied_cannot_mutate_through_ptb(home, monkeypatch):
    from clover_cli import config
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    raw = config.require_readable_config_before_write()
    raw.pop("proactive")
    config.save_config(raw, strip_defaults=False, strip_managed=False)
    before = (home / "config.yaml").read_bytes()
    async with app:
        await dispatch(app, a, command(app, "/proactive"))
        assert not load_settings().enabled
        assert (home / "config.yaml").read_bytes() == before
        sends = len([p for m, p in endpoint.calls if m == "sendMessage"])
        await dispatch(app, a, command(app, "/proactive on", uid=22222222))
        assert len([p for m, p in endpoint.calls if m == "sendMessage"]) == sends
        assert (home / "config.yaml").read_bytes() == before
        assert not load_settings().enabled
    errors.assert_not_called()


@pytest.mark.asyncio
async def test_pre_upgrade_profile_without_owner_record_needs_no_enrollment(home, monkeypatch):
    from clover_cli import config
    from gateway.config import load_gateway_config
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    legacy = {"platforms": {"telegram": {"enabled": True, "allow_from": [UID]}},
              "proactive": {"enabled": False, "mode": "active", "suggestions": {"enabled": False}},
              "display": {"show_reasoning": False}, "model": {"default": "pre-upgrade-model"}}
    (home / "config.yaml").write_text(json.dumps(legacy))
    r.config = load_gateway_config()
    a.config = r.config.platforms[Platform.TELEGRAM]
    assert not VerifiedOwnerStore(TOKEN, home=home).path.exists()
    assert not load_settings().enabled
    baseline = config.require_readable_config_before_write()
    before = (home / "config.yaml").read_bytes()
    async with app:
        await dispatch(app, a, command(app, "/proactive"))
        menu = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
        assert "reply_markup" in menu
        assert (home / "config.yaml").read_bytes() == before
        assert not any(word in menu["text"].lower() for word in ("owner", "pairing", "terminal", "setup"))
        await app.process_update(callback(app, menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"]))
        assert load_settings().enabled
        saved = config.require_readable_config_before_write()
        assert saved == {**baseline, "proactive": {**baseline["proactive"], "enabled": True}}
        assert not VerifiedOwnerStore(TOKEN, home=home).path.exists()
    errors.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_group_serialized_callback_obeys_current_group_admin_scope(home, monkeypatch, revoked):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    a.config.extra.update(allow_admin_from=[UID], group_allow_from=[UID], require_mention=False,
                          group_allow_admin_from=[UID], group_user_allowed_commands=["proactive"])
    async with app:
        await dispatch(app, a, command(app, "/proactive", kind="supergroup"))
        menu = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
        if revoked:
            a.config.extra["group_allow_admin_from"] = ["87654321"]
        before = (home / "config.yaml").read_bytes()
        await app.process_update(callback(app, menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                                          chat=-10077, kind="supergroup"))
        assert load_settings().enabled is (not revoked)
        if revoked:
            assert (home / "config.yaml").read_bytes() == before
            assert not [p for m, p in endpoint.calls if m == "editMessageText"]
    errors.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["source", "chat", "mention"])
async def test_unrestricted_group_command_gate_never_bypasses_group_intake_policy(home, monkeypatch, blocked):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    a.config.extra.update(group_allow_from=[UID], require_mention=False)
    if blocked == "source": a.config.extra["group_allow_from"] = ["87654321"]
    if blocked == "chat": a.config.extra["allowed_chats"] = ["-10088"]
    if blocked == "mention": a.config.extra["require_mention"] = True
    before = (home / "config.yaml").read_bytes()
    async with app:
        await dispatch(app, a, command(app, "/proactive on", kind="supergroup"))
        assert not load_settings().enabled
        assert (home / "config.yaml").read_bytes() == before
        assert not [p for m, p in endpoint.calls if m in {"sendMessage", "editMessageText"}]
    errors.assert_not_called()


@pytest.mark.asyncio
async def test_revoking_legacy_owner_record_preserves_independent_command_authority(home, monkeypatch):
    from tests.test_owner_proactive_controls import verify
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    store = verify(home)
    async with app:
        await dispatch(app, a, command(app, "/proactive"))
        menu = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
        store.revoke()
        assert not store.is_owner(UID)
        await app.process_update(callback(app, menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"]))
        assert load_settings().enabled
    errors.assert_not_called()

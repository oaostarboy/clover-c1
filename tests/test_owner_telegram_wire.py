"""Genuine PTB update and send wire proof; only the HTTP endpoint is synthetic."""
import json
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest

from gateway.config import PlatformConfig
from gateway.pairing import VerifiedOwnerStore
from plugins.platforms.telegram.adapter import TelegramAdapter

TOKEN = "123456:test-token"


class TelegramEndpoint(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        params = request_data.parameters if request_data else {}
        self.calls.append((url.rsplit('/', 1)[-1], params))
        if url.endswith("getMe"):
            result = {"id": 123456, "is_bot": True, "first_name": "Bot", "username": "testbot"}
        else:
            result = {"message_id": 50, "date": 1800000000,
                      "chat": {"id": int(params.get("chat_id", 12345678)), "type": "private"},
                      "text": params.get("text", "")}
        return 200, json.dumps({"ok": True, "result": result}).encode()


def update(app, text, *, uid=12345678, kind="private", bot=False, forwarded=False):
    msg = {"message_id": 17, "date": 1800000000, "text": text,
           "chat": {"id": uid if kind == "private" else -10077, "type": kind},
           "from": {"id": uid, "is_bot": bot, "first_name": "Untrusted"},
           "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}
    if forwarded:
        msg["forward_origin"] = {"type": "hidden_user", "date": 1800000000, "sender_user_name": "Owner"}
    return Update.de_json({"update_id": 1, "message": msg}, app.bot)


@pytest.mark.asyncio
async def test_private_transport_proof_never_dispatches_model_or_grants_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    s = VerifiedOwnerStore(TOKEN)
    challenge = s.begin()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN, extra={"allow_from": ["99999"]}))
    a._bot = app.bot
    a._message_handler = AsyncMock(side_effect=AssertionError("owner proof must not reach model dispatch"))
    a._is_user_authorized_from_message = lambda msg: False  # Dedicated proof is not chat admission.
    a._register_handlers(app)
    async with app:
        await app.process_update(update(app, f"/start cloverowner_{challenge}"))
    sends = [params for method, params in endpoint.calls if method == "sendMessage"]
    assert len(sends) == 1, "local owner ceremony needs a transport proof even before guest admission"
    assert "not yet" in sends[0]["text"].lower()
    code = sends[0]["text"].split("Confirmation code: ", 1)[1].split()[0]
    assert challenge not in sends[0]["text"]
    assert not s.is_owner("12345678")
    assert s.confirm(code) == "12345678"
    assert VerifiedOwnerStore(TOKEN).is_owner("12345678")
    a._message_handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["group", "bot", "forwarded", "forged", "cross-bot", "cross-profile", "expired", "save-failed"])
async def test_untrusted_or_unpersisted_proof_is_silent_and_never_dispatches(tmp_path, monkeypatch, caplog, bad):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    s = VerifiedOwnerStore(TOKEN)
    challenge = s.begin()
    before = s.path.read_bytes()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    a._bot = app.bot
    a._message_handler = AsyncMock(side_effect=AssertionError("proof reached model"))
    kwargs = {}
    if bad == "group": kwargs["kind"] = "supergroup"
    if bad == "bot": kwargs["bot"] = True
    if bad == "forwarded": kwargs["forwarded"] = True
    if bad == "forged": challenge = "a" * 32
    if bad == "cross-bot": a.config.token = "654321:different-token"
    if bad == "cross-profile":
        a._owner_verification_home = tmp_path / "different"
    if bad == "expired": monkeypatch.setattr("gateway.pairing.time.time", lambda: json.loads(s.path.read_text())["pending"]["expires"])
    if bad == "save-failed":
        monkeypatch.setattr("gateway.pairing._secure_write", lambda *args: None)
    a._register_handlers(app)
    async with app:
        await app.process_update(update(app, f"/start cloverowner_{challenge}", **kwargs))
    assert not [p for m, p in endpoint.calls if m == "sendMessage"]
    assert not s.is_owner("12345678")
    assert s.path.read_bytes() == before
    assert challenge not in caplog.text
    a._message_handler.assert_not_called()


@pytest.mark.asyncio
async def test_owner_proof_secrets_are_not_in_debug_logs(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    s = VerifiedOwnerStore(TOKEN)
    challenge = s.begin()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    a._bot = app.bot
    a._register_handlers(app)
    caplog.set_level(logging.DEBUG, logger="telegram")
    async with app:
        await app.process_update(update(app, f"/start cloverowner_{challenge}"))
    sends = [p for m, p in endpoint.calls if m == "sendMessage"]
    code = sends[0]["text"].split("Confirmation code: ", 1)[1].split()[0]
    assert challenge not in caplog.text
    assert code not in caplog.text


@pytest.mark.asyncio
async def test_failed_owner_reply_does_not_leak_codes_in_error_handler(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    s = VerifiedOwnerStore(TOKEN)
    challenge = s.begin()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    a._bot = app.bot
    a._register_handlers(app)
    errors = AsyncMock()
    app.add_error_handler(errors)

    async def fail(**kwargs):
        raise RuntimeError(kwargs["text"])

    monkeypatch.setattr(type(app.bot), "send_message", lambda self, **kwargs: fail(**kwargs))
    async with app:
        await app.process_update(update(app, "/start cloverowner_" + challenge))
    errors.assert_not_called()
    assert challenge not in caplog.text
    assert not s.is_owner("12345678")


@pytest.mark.asyncio
async def test_command_addressed_to_another_bot_does_not_bind_account(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    s = VerifiedOwnerStore(TOKEN)
    challenge = s.begin()
    before = s.path.read_bytes()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    a._bot = app.bot
    a._register_handlers(app)
    async with app:
        await app.process_update(update(app, "/start@otherbot cloverowner_" + challenge))
    assert not [p for m, p in endpoint.calls if m == "sendMessage"]
    assert s.path.read_bytes() == before


@pytest.mark.asyncio
async def test_proof_intake_is_pinned_to_credential_profile_not_ambient_task_home(tmp_path, monkeypatch):
    original = tmp_path / "credential-profile"
    ambient = tmp_path / "ambient-profile"
    monkeypatch.setenv("CLOVER_HOME", str(original))
    s = VerifiedOwnerStore(TOKEN)
    challenge = s.begin()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    a._bot = app.bot
    a._register_handlers(app)
    monkeypatch.setenv("CLOVER_HOME", str(ambient))
    async with app:
        await app.process_update(update(app, "/start cloverowner_" + challenge))
    text = [p["text"] for m, p in endpoint.calls if m == "sendMessage"][0]
    code = text.split("Confirmation code: ", 1)[1].split()[0]
    assert not VerifiedOwnerStore(TOKEN, home=ambient).path.exists()
    assert not s.is_owner("12345678")
    assert s.confirm(code) == "12345678"


@pytest.mark.asyncio
@pytest.mark.parametrize("quoted", [False, True])
async def test_owner_challenges_in_plain_text_never_enter_chat_batch_or_history(tmp_path, monkeypatch, quoted):
    from unittest.mock import Mock
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    s = VerifiedOwnerStore(TOKEN)
    challenge = s.begin()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    a._bot = app.bot
    a._enqueue_text_event = Mock()
    a._is_user_authorized_from_message = lambda message: True
    a._register_handlers(app)
    text = ("Quoted setup: " if quoted else " ") + "/start cloverowner_" + challenge
    data = update(app, text).to_dict()
    data["message"].pop("entities")
    raw = Update.de_json(data, app.bot)
    async with app:
        await app.process_update(raw)
    a._enqueue_text_event.assert_not_called()
    sends = [p for m, p in endpoint.calls if m == "sendMessage"]
    assert len(sends) == (0 if quoted else 1)
    assert not s.is_owner("12345678")


@pytest.mark.asyncio
async def test_verified_owner_native_picker_and_callback_use_real_ptb_http_serialization(tmp_path, monkeypatch):
    from tests.test_owner_proactive_controls import transport, verify, UID
    from tests.gateway.test_proactive_command import event
    from agent.workstreams import load_settings
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    r, a = transport(tmp_path, monkeypatch)
    verify(tmp_path)
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a._bot = app.bot
    # Restore the actual send seam: the only replacement is the HTTP endpoint.
    a._send_message_with_thread_fallback = TelegramAdapter._send_message_with_thread_fallback.__get__(a)
    a._register_handlers(app)
    before = (tmp_path / "config.yaml").read_bytes()
    async with app:
        assert await r._handle_message(event("/proactive", UID, chat_id=UID)) is None
        sends = [p for m, p in endpoint.calls if m == "sendMessage"]
        assert len(sends) == 1
        markup = sends[0]["reply_markup"]
        assert isinstance(markup, dict)
        assert [b["text"] for b in markup["inline_keyboard"][0]] == ["Enable", "Disable"]
        json.dumps(markup)  # Not a MagicMock-shaped UI.
        print("OWNER_REAL_PTB_WIRE=" + json.dumps({"text": sends[0]["text"], "reply_markup": markup}))
        assert not load_settings().enabled
        assert (tmp_path / "config.yaml").read_bytes() == before
        callback = Update.de_json({"update_id": 2, "callback_query": {
            "id": "tap-enable", "chat_instance": "one", "data": markup["inline_keyboard"][0][0]["callback_data"],
            "from": {"id": int(UID), "is_bot": False, "first_name": "Owner"},
            "message": {"message_id": 50, "date": 1800000000,
                        "chat": {"id": int(UID), "type": "private"}, "text": "Proactive: Off"},
        }}, app.bot)
        await app.process_update(callback)
        assert load_settings().enabled
        edits = [p for m, p in endpoint.calls if m == "editMessageText"]
        assert len(edits) == 1 and "Saved" in edits[0]["text"]
        await app.process_update(callback)  # Consumed picker cannot mutate again.
        assert len([p for m, p in endpoint.calls if m == "editMessageText"]) == 1
    r._handle_message_with_agent.assert_not_called()

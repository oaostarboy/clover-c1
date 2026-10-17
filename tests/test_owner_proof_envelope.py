"""Independent production ingress adversarial probes; no candidate source edits."""
import json
from unittest.mock import Mock

import pytest
from telegram import Update
from telegram.ext import Application

from gateway.config import PlatformConfig
from gateway.pairing import VerifiedOwnerStore
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.test_owner_telegram_wire import TOKEN, TelegramEndpoint, update



def raw_message(app, message, seq):
    return Update.de_json({"update_id": seq, "message": message}, app.bot)


def base_message(text="hello", mid=18):
    return {"message_id": mid, "date": 1800000000, "text": text,
            "chat": {"id": 12345678, "type": "private"},
            "from": {"id": 12345678, "is_bot": False, "first_name": "Owner"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["challenge-reply", "confirmation-reply", "challenge-quote"])
async def test_reply_context_cannot_reintroduce_owner_proofs(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    s = VerifiedOwnerStore(TOKEN)
    challenge = s.begin()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN, extra={"allow_from": ["12345678"]}))
    a._bot = app.bot
    enqueue = Mock()
    a._enqueue_text_event = enqueue
    a._register_handlers(app)
    async with app:
        # Positive real command baseline: reserved direct proof never enters chat.
        await app.process_update(update(app, "/start cloverowner_" + challenge))
        enqueue.assert_not_called()
        sends = [p for m, p in endpoint.calls if m == "sendMessage"]
        assert len(sends) == 1
        code = sends[0]["text"].split("Confirmation code: ", 1)[1].split()[0]
        assert not s.is_owner("12345678")
        # Positive ordinary reply baseline proves this admitted route is active.
        ordinary = base_message("continue")
        ordinary["reply_to_message"] = base_message("ordinary prior message", mid=17)
        await app.process_update(raw_message(app, ordinary, 2))
        assert enqueue.call_count == 1
        assert enqueue.call_args.args[0].reply_to_text == "ordinary prior message"
        enqueue.reset_mock()
        # Change only reply/quote content, not actor, admission or routing.
        changed = base_message("continue")
        prior = base_message("/start cloverowner_" + challenge, mid=17)
        secret = challenge
        if kind == "confirmation-reply":
            prior["text"] = sends[0]["text"]
            prior["from"] = {"id": 123456, "is_bot": True, "first_name": "Bot"}
            secret = code
        changed["reply_to_message"] = prior
        if kind == "challenge-quote":
            changed["quote"] = {"text": prior["text"], "position": 0}
        await app.process_update(raw_message(app, changed, 3))
        assert not enqueue.called or secret not in (enqueue.call_args.args[0].reply_to_text or ""), (
            "owner proof reached admitted chat event reply context: " + kind)


class PhotoEndpoint(TelegramEndpoint):
    def __init__(self):
        import io
        from PIL import Image
        super().__init__()
        buffer = io.BytesIO()
        Image.new("RGB", (1, 1), "white").save(buffer, format="JPEG")
        self.photo_bytes = buffer.getvalue()

    async def do_request(self, url, method, request_data=None, **kwargs):
        if url.endswith("getFile"):
            self.calls.append(("getFile", request_data.parameters))
            result = {"file_id": "photo1", "file_unique_id": "unique1", "file_path": "photo.jpg", "file_size": 4}
            return 200, json.dumps({"ok": True, "result": result}).encode()
        if url.endswith("photo.jpg"):
            return 200, self.photo_bytes
        return await super().do_request(url, method, request_data, **kwargs)


@pytest.mark.asyncio
async def test_photo_caption_cannot_reintroduce_owner_challenge(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    s = VerifiedOwnerStore(TOKEN)
    challenge = s.begin()
    endpoint = PhotoEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN, extra={"allow_from": ["12345678"]}))
    a._bot = app.bot
    enqueue = Mock()
    a._enqueue_photo_event = enqueue
    a._register_handlers(app)
    def photo(caption):
        msg = base_message()
        msg.pop("text")
        msg["caption"] = caption
        msg["photo"] = [{"file_id": "photo1", "file_unique_id": "unique1", "width": 1, "height": 1, "file_size": 4}]
        return msg
    async with app:
        # Positive ordinary photo baseline exercises real getFile/cache/queue.
        await app.process_update(raw_message(app, photo("ordinary caption"), 4))
        assert enqueue.call_count == 1
        assert enqueue.call_args.args[1].text == "ordinary caption"
        enqueue.reset_mock()
        await app.process_update(raw_message(app, photo("/start cloverowner_" + challenge), 5))
        assert not enqueue.called or challenge not in enqueue.call_args.args[1].text, (
            "owner challenge reached admitted media event caption")




@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["text", "caption", "reply-caption", "quote", "edit-text", "edit-caption", "location-reply"])
@pytest.mark.parametrize("proof", ["challenge", "confirmation"])
async def test_all_textual_envelopes_stop_before_chat_and_platform_observer(tmp_path, monkeypatch, route, proof):
    from unittest.mock import AsyncMock
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    store = VerifiedOwnerStore(TOKEN)
    challenge = store.begin()
    code = store.prove(challenge, "87654321")
    secret = "cloverowner_" + challenge if proof == "challenge" else "Confirmation code: " + code
    before = store.path.read_bytes()
    endpoint = PhotoEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN, extra={"allow_from": ["12345678"]}))
    adapter._bot = app.bot
    adapter._enqueue_text_event = Mock()
    adapter._enqueue_photo_event = Mock()
    adapter.handle_message = AsyncMock()
    adapter._platform_event_handler = AsyncMock()
    monkeypatch.setattr("clover_cli.lifecycle.has_hook", lambda name: True)
    adapter._register_handlers(app)

    def envelope(content, seq):
        msg = base_message("continue", mid=seq)
        if route in ("text", "edit-text"):
            msg["text"] = content
        elif route in ("caption", "edit-caption"):
            msg.pop("text")
            msg["caption"] = content
            msg["photo"] = [{"file_id": "photo1", "file_unique_id": "unique1", "width": 1, "height": 1}]
        elif route == "reply-caption":
            prior = base_message(mid=1)
            prior.pop("text")
            prior["caption"] = content
            msg["reply_to_message"] = prior
        elif route == "quote":
            msg["reply_to_message"] = base_message("ordinary prior", mid=1)
            msg["quote"] = {"text": content, "position": 0}
        elif route == "location-reply":
            msg.pop("text")
            msg["location"] = {"latitude": 1, "longitude": 2}
            msg["reply_to_message"] = base_message(content, mid=1)
        key = "edited_message" if route.startswith("edit-") else "message"
        return Update.de_json({"update_id": seq, key: msg}, app.bot)

    async with app:
        await app.process_update(envelope("ordinary content", 31))
        sinks = [adapter._enqueue_text_event, adapter._enqueue_photo_event, adapter.handle_message, adapter._platform_event_handler]
        assert any(sink.called for sink in sinks), "positive admitted route must work before mutation"
        if route.startswith("edit-"):
            assert adapter._platform_event_handler.await_count == 1
            assert adapter._platform_event_handler.call_args.args[0]["payload"]["text"] == "ordinary content"
        for sink in sinks:
            sink.reset_mock()
        calls_before = len(endpoint.calls)
        await app.process_update(envelope(secret, 32))
        assert not any(sink.called for sink in sinks), "proof entered chat, media, location or edited-event observer"
        assert len(endpoint.calls) == calls_before, "proof must not start media download or send confirmation"
    assert store.path.read_bytes() == before
    assert not store.is_owner("12345678")


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [False, True])
async def test_unmentioned_group_proof_never_enters_observed_history(tmp_path, monkeypatch, media):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    endpoint = PhotoEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    adapter._bot = app.bot
    adapter._is_user_authorized_from_message = lambda message: True
    adapter._should_process_message = lambda message: False
    adapter._should_observe_unmentioned_group_message = lambda message: True
    adapter._observe_unmentioned_group_message = Mock()
    adapter._register_handlers(app)
    def group_message(content, seq):
        msg = base_message(content, mid=seq)
        msg["chat"] = {"id": -10077, "type": "supergroup", "title": "Guests"}
        if media:
            msg.pop("text")
            msg["caption"] = content
            msg["photo"] = [{"file_id": "photo1", "file_unique_id": "unique1", "width": 1, "height": 1}]
        return raw_message(app, msg, seq)
    async with app:
        await app.process_update(group_message("ordinary caption", 41))
        assert adapter._observe_unmentioned_group_message.call_count == 1
        adapter._observe_unmentioned_group_message.reset_mock()
        calls_before = len(endpoint.calls)
        await app.process_update(group_message("Confirmation code: 1234567890abcdef", 42))
        adapter._observe_unmentioned_group_message.assert_not_called()
        assert len(endpoint.calls) == calls_before


@pytest.mark.asyncio
async def test_consumed_proof_releases_opt_in_update_admission_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    adapter._bot = app.bot
    adapter._update_admission = True
    adapter._durable_update_receipts = False
    adapter._enqueue_text_event = Mock()
    adapter._register_handlers(app)
    async with app:
        await app.process_update(raw_message(app, base_message("hello"), 51))
        assert adapter._enqueue_text_event.call_count == 1
        assert adapter._inflight_update_ids  # Real admitted event owns this claim.
        prior = set(adapter._inflight_update_ids)
        await app.process_update(raw_message(app, base_message("Confirmation code: 1234567890abcdef"), 52))
        assert set(adapter._inflight_update_ids) == prior, "consumed proof left a dangling admission claim"
        assert not adapter._seen_update_ids, "discarded proof is not persisted as a chat receipt"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["store", "identity"])
async def test_unexpected_proof_error_cannot_escape_into_ptb_error_handlers(tmp_path, monkeypatch, caplog, fault):
    from unittest.mock import AsyncMock
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    store = VerifiedOwnerStore(TOKEN)
    challenge = store.begin()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    adapter._bot = app.bot
    adapter._register_handlers(app)
    errors = AsyncMock()
    app.add_error_handler(errors)
    async with app:
        await app.process_update(update(app, "/start cloverowner_" + challenge))
        assert len([p for m, p in endpoint.calls if m == "sendMessage"]) == 1
        def failed(*args):
            raise LookupError("cloverowner_" + challenge)
        if fault == "store":
            monkeypatch.setattr(VerifiedOwnerStore, "prove", failed)
        else:
            monkeypatch.setattr(type(app.bot), "username", property(failed))
        await app.process_update(update(app, "/start@testbot cloverowner_" + challenge))
    errors.assert_not_called()
    assert challenge not in caplog.text
    assert not store.is_owner("12345678")


def test_transport_and_ingestion_exception_logs_redact_proof_tracebacks(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    challenge = "x" * 32
    code = "1234567890abcdef"
    for name in ("telegram.ext.Application", "plugins.platforms.telegram.adapter"):
        target = logging.getLogger(name)
        target.warning("ordinary failure")
        assert "ordinary failure" in caplog.text
        caplog.clear()
        try:
            raise RuntimeError("cloverowner_" + challenge + " Confirmation code: " + code)
        except RuntimeError:
            target.exception("failed proof envelope")
        assert challenge not in caplog.text
        assert code not in caplog.text
        assert "RuntimeError" in caplog.text
        caplog.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [False, True])
@pytest.mark.parametrize("proof", ["challenge", "confirmation"])
async def test_real_batch_flush_keeps_proofs_out_of_gateway_info_and_model_entry(tmp_path, monkeypatch, caplog, media, proof):
    import asyncio
    import logging
    from types import SimpleNamespace
    from gateway.run import GatewayRunner
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    store = VerifiedOwnerStore(TOKEN)
    challenge = store.begin()
    code = store.prove(challenge, "87654321")
    assert code
    secret_text = "cloverowner_" + challenge if proof == "challenge" else "Confirmation code: " + code
    secret = challenge if proof == "challenge" else code
    endpoint = PhotoEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN, extra={"allow_from": ["12345678"]}))
    adapter._bot = app.bot
    adapter._text_batch_delay_seconds = 0.01
    adapter._media_batch_delay_seconds = 0.01
    received = []
    def stop_after_log(source):
        raise RuntimeError("stop-before-session-and-model")
    sink = SimpleNamespace(_recover_telegram_topic_thread_id=stop_after_log)
    async def gateway_entry(event):
        received.append(event)
        with pytest.raises(RuntimeError, match="stop-before-session-and-model"):
            await GatewayRunner._handle_message_with_agent(sink, event, event.source, "offline", 0)
    adapter.handle_message = gateway_entry
    adapter._register_handlers(app)
    caplog.set_level(logging.INFO, logger="gateway.run")
    def message(content, seq):
        msg = base_message("continue", mid=seq)
        if media:
            msg.pop("text")
            msg["caption"] = content
            msg["photo"] = [{"file_id": "photo1", "file_unique_id": "unique1", "width": 1, "height": 1}]
        else:
            msg["reply_to_message"] = base_message(content, mid=1)
        return raw_message(app, msg, seq)
    async def drain():
        tasks = list(adapter._pending_text_batch_tasks.values()) + list(adapter._pending_photo_batch_tasks.values())
        await asyncio.gather(*tasks)
    async with app:
        await app.process_update(message("ordinary prior message", 61))
        await drain()
        assert len(received) == 1
        assert "ordinary prior message" in caplog.text
        received.clear()
        caplog.clear()
        # With no existing batch, a reintroduced reply reaches the genuine
        # gateway INFO sink on the rejected source (not just an enqueue mock).
        await app.process_update(message(secret_text, 62))
        await drain()
        assert not received, "proof was dispatched by the real batch flush"
        assert secret not in caplog.text
        # An ordinary batch already pending must survive the isolated proof.
        await app.process_update(message("ordinary prior message", 63))
        await app.process_update(message(secret_text, 64))
        await drain()
        assert len(received) == 1
        event = received[0]
        assert secret not in event.text + (event.reply_to_text or "") + str(event.raw_message)
        assert secret not in caplog.text
        assert not adapter._pending_text_batches and not adapter._pending_photo_batches


@pytest.mark.asyncio
async def test_edited_direct_start_cannot_submit_a_new_claimant(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    store = VerifiedOwnerStore(TOKEN)
    challenge = store.begin()
    before = store.path.read_bytes()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
    adapter._bot = app.bot
    adapter._enqueue_text_event = Mock()
    adapter._register_handlers(app)
    async with app:
        ordinary = base_message("ordinary text")
        await app.process_update(Update.de_json({"update_id": 71, "edited_message": ordinary}, app.bot))
        assert adapter._enqueue_text_event.call_count == 1
        adapter._enqueue_text_event.reset_mock()
        edited = update(app, "/start cloverowner_" + challenge).to_dict()["message"]
        await app.process_update(Update.de_json({"update_id": 72, "edited_message": edited}, app.bot))
        adapter._enqueue_text_event.assert_not_called()
    assert store.path.read_bytes() == before
    assert not [p for m, p in endpoint.calls if m == "sendMessage"]


def test_transport_error_surface_redacts_the_same_proof_markers():
    from plugins.platforms.telegram.adapter import _redact_telegram_error_text
    assert _redact_telegram_error_text("ordinary error") == "ordinary error"
    result = _redact_telegram_error_text("cloverowner_" + "x" * 32 + " Confirmation code: 1234567890abcdef")
    assert "x" * 32 not in result
    assert "1234567890abcdef" not in result

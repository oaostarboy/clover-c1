"""Real PTB cancellation/replay regression adapted from SEC1-CLEANUP review.

Only the HTTP endpoint and send fault are synthetic; admission, proof/store,
registered dispatch, finalization and event receipts are production paths.
"""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Update
from telegram.ext import Application

from gateway.config import PlatformConfig
from gateway.pairing import VerifiedOwnerStore
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.update_admission import make_finalize_handler
from tests.test_owner_telegram_wire import TOKEN, TelegramEndpoint, update as wire_update


def update(app, text, *, update_id):
    raw = wire_update(app, text).to_dict()
    raw["update_id"] = update_id
    if not text.startswith("/start"):
        raw["message"].pop("entities", None)
    return Update.de_json(raw, app.bot)


def configured_adapter(app, *, durable):
    adapter = TelegramAdapter(PlatformConfig(
        enabled=True, token=TOKEN, extra={"allow_from": ["12345678"]},
    ))
    adapter._bot = app.bot
    adapter._update_admission = True
    adapter._durable_update_receipts = durable
    adapter._enqueue_text_event = Mock()
    adapter._platform_event_handler = AsyncMock()
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("fault", ["exception", "cancellation", "success"])
async def test_proof_reply_fault_releases_admission_claim(tmp_path, monkeypatch, caplog, fault, durable):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    store = VerifiedOwnerStore(TOKEN)
    challenge = store.begin()
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    adapter = configured_adapter(app, durable=durable)
    adapter._register_handlers(app)
    errors = AsyncMock()
    app.add_error_handler(errors)
    async with app:
        # Real successful proof and ordinary guest path before any fault.
        await app.process_update(update(app, "/start cloverowner_" + challenge, update_id=101))
        assert not adapter._inflight_update_ids
        assert [p for m, p in endpoint.calls if m == "sendMessage"]
        assert not store.owner_user_id()
        await app.process_update(update(app, "ordinary control", update_id=102))
        assert adapter._enqueue_text_event.call_count == 1
        ordinary_event = adapter._enqueue_text_event.call_args.args[0]
        # Leave this unrelated event-owned claim pending throughout proof cleanup.
        ordinary_claims = dict(adapter._inflight_update_ids)
        assert set(ordinary_claims) == adapter._inflight_with_event == {"123456:102"}
        adapter._enqueue_text_event.reset_mock()
        adapter._platform_event_handler.reset_mock()
        caplog.clear()
        entered = asyncio.Event()
        original = type(app.bot).send_message
        reply_codes = []

        async def faulting_send(self, *args, **kwargs):
            reply_codes.append(kwargs["text"].split("Confirmation code: ", 1)[1].split()[0])
            if fault == "exception":
                raise LookupError("cloverowner_" + challenge)
            if fault == "cancellation":
                entered.set()
                await asyncio.Event().wait()
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(type(app.bot), "send_message", faulting_send)
        # New challenge reaches a genuine send await after persisted store.prove.
        challenge = store.begin()
        proof_update = update(app, "/start cloverowner_" + challenge, update_id=103)
        task = asyncio.create_task(app.process_update(proof_update))
        if fault == "cancellation":
            try:
                await asyncio.wait_for(entered.wait(), 5)
            finally:
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled(), "cleanup must not swallow cancellation"
        else:
            await task
        assert len(reply_codes) == 1
        assert list(json.loads(store.path.read_text())["pending"]["proofs"].values()) == ["12345678"]
        assert not store.owner_user_id()
        errors.assert_not_called()
        adapter._enqueue_text_event.assert_not_called()
        adapter._platform_event_handler.assert_not_called()
        assert challenge not in caplog.text
        assert reply_codes[0] not in caplog.text
        assert not adapter._seen_update_ids
        assert not list(adapter._update_receipt_dir.glob("telegram_update_receipts_*.json"))
        remaining_proof_claims = set(adapter._inflight_update_ids) - set(ordinary_claims)
        assert {key: adapter._inflight_update_ids.get(key) for key in ordinary_claims} == ordinary_claims
        assert adapter._inflight_with_event == set(ordinary_claims)

        monkeypatch.setattr(type(app.bot), "send_message", original)
        proof_calls = AsyncMock(wraps=adapter._handle_owner_verification)
        monkeypatch.setattr(adapter, "_handle_owner_verification", proof_calls)
        # Use the identical PTB update, not a different update ID or new proof.
        await app.process_update(proof_update)
        replay_calls = proof_calls.await_count
        assert not remaining_proof_claims, "proof fault retained a claim and suppresses replay"
        assert replay_calls == 1, "the no-event proof replay must be admitted after a fault"
        # A second cleanup is harmless; it must leave ordinary event ownership alone.
        finalize = make_finalize_handler(adapter, 123456)
        await finalize(proof_update, None)
        await finalize(proof_update, None)
        assert adapter._inflight_update_ids == ordinary_claims
        assert adapter._inflight_with_event == set(ordinary_claims)
        assert not adapter._inflight_failed
        assert not adapter._seen_update_ids
        errors.assert_not_called()
        adapter._enqueue_text_event.assert_not_called()
        adapter._platform_event_handler.assert_not_called()
        assert challenge not in caplog.text and reply_codes[0] not in caplog.text
        assert not store.owner_user_id()
        assert not list(adapter._update_receipt_dir.glob("telegram_update_receipts_*.json"))

        await app.process_update(update(app, "ordinary control after fault", update_id=104))
        assert adapter._enqueue_text_event.call_count == 1, "fault must not disable other ordinary input"
        next_event = adapter._enqueue_text_event.call_args.args[0]
        for event in (ordinary_event, next_event):
            for receipt in event.inbound_receipts:
                receipt.release()
        assert not adapter._inflight_update_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_nonproof_cancellation_keeps_event_owned_claim(tmp_path, monkeypatch, durable):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    adapter = configured_adapter(app, durable=durable)
    entered = asyncio.Event()

    async def blocked_observer(update, context):
        entered.set()
        await asyncio.Event().wait()

    # Cancel ordinary dispatch after the real text handler attached its receipt.
    adapter._on_platform_update = blocked_observer
    adapter._register_handlers(app)
    errors = AsyncMock()
    app.add_error_handler(errors)
    async with app:
        ordinary = update(app, "ordinary cancellation control", update_id=201)
        task = asyncio.create_task(app.process_update(ordinary))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        event = adapter._enqueue_text_event.call_args.args[0]
        assert len(event.inbound_receipts) == 1
        claims = dict(adapter._inflight_update_ids)
        assert set(claims) == adapter._inflight_with_event == {"123456:201"}
        # Nonproof interception must return without taking ownership of cleanup.
        await adapter._intercept_owner_proof_update(ordinary, None)
        finalize = make_finalize_handler(adapter, 123456)
        await finalize(ordinary, None)
        await finalize(ordinary, None)
        assert adapter._inflight_update_ids == claims
        assert adapter._inflight_with_event == set(claims)
        assert not adapter._seen_update_ids
        assert not list(adapter._update_receipt_dir.glob("telegram_update_receipts_*.json"))
        # Same-update replay remains suppressed while the ordinary event owns it.
        await app.process_update(ordinary)
        assert adapter._enqueue_text_event.call_count == 1
        errors.assert_not_called()
        event.inbound_receipts[0].release()
        assert not adapter._inflight_update_ids
        assert not adapter._inflight_with_event
        await finalize(ordinary, None)
        assert not adapter._seen_update_ids

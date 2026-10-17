"""Real PTB Application processing, admission, sends, recovery and exact retry."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from telegram import Update
from telegram.ext import Application, MessageHandler, TypeHandler, filters

from gateway.platforms.base import BasePlatformAdapter
from plugins.platforms.telegram.update_admission import make_admission_handler, make_finalize_handler
from tests.test_telegram_album_queue_ownership import photo, replay  # noqa: F401
from tests.test_independent_telegram_album_review import finish_timers, waiting


@asynccontextmanager
async def native_app(x):
    a = x.adapter
    a._update_admission = True
    a._bot = x.bot
    a._send_with_retry = BasePlatformAdapter._send_with_retry.__get__(a)
    app = Application.builder().bot(x.bot).updater(None).build()
    app.add_handler(TypeHandler(Update, make_admission_handler(a, '123')), group=-100)
    app.add_handler(MessageHandler(filters.PHOTO, a._handle_media_message), group=0)
    app.add_handler(MessageHandler(filters.TEXT, a._handle_text_message), group=0)
    app.add_handler(TypeHandler(Update, make_finalize_handler(a, '123')), group=100)
    errors = []
    async def error(update, context):
        errors.append(context.error)
    app.add_error_handler(error)
    await app.initialize()
    try:
        yield app
        assert not errors
    finally:
        await app.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['handler_error', 'cancel_before', 'cancel_after'])
async def test_application_exact_update_recovery_keeps_one_request(replay, failure):
    x = replay
    a = x.adapter
    handler = a._message_handler
    entered = asyncio.Event()
    attempts = []
    request = x.bot.request
    send = request.do_request
    async def wire(url, method, request_data=None, **kwargs):
        if url.endswith('/sendMessage'):
            attempts.append(dict(request_data.parameters))
            if failure == 'cancel_after':
                entered.set()
                await asyncio.Event().wait()
        return await send(url, method, request_data=request_data, **kwargs)
    request.do_request = wire
    async def broken(event):
        entered.set()
        if failure == 'handler_error':
            raise RuntimeError('known before queue handoff')
        await asyncio.Event().wait()
    if failure != 'cancel_after':
        a._message_handler = broken
    update = photo(x.bot, 20, '/queue pricing')
    async with native_app(x) as app:
        await app.process_update(update)
        await asyncio.wait_for(entered.wait(), 2)
        if failure == 'handler_error':
            await finish_timers(a)
        else:
            a._drop_delayed_deliveries = True
            await a._cancel_pending_delivery_tasks()
        if failure == 'cancel_after':
            assert len(waiting(x)) == 1 and not a._held_inbound_events
            assert len(attempts) == 1 and not request.sent
        else:
            assert not waiting(x) and len(a._held_inbound_events) == 1
            assert len(a._inflight_update_ids) == 1
            # The exact update remains event-owned, so a duplicate cannot enter.
            await app.process_update(update)
            assert not a._media_group_tasks
        a._message_handler = handler
        a._drop_delayed_deliveries = False
        await a._redispatch_held_inbound()
        assert not a._inflight_update_ids
        # An in-memory acceptance never fabricates a durable receipt.
        assert not a._seen_update_ids
        await app.process_update(update)
        await finish_timers(a)
        assert [e.text for e in waiting(x)] == ['pricing']
        assert [len(e.media_urls) for e in waiting(x)] == [1]
        assert len(attempts) == 1
        assert len(request.sent) == (0 if failure == 'cancel_after' else 1)
        assert not a._inflight_update_ids and not a._held_inbound_events
        assert not x.active.done()


@pytest.mark.asyncio
async def test_application_success_owns_caption_and_two_images(replay):
    from agent.image_routing import build_native_content_parts
    x = replay
    async with native_app(x) as app:
        await app.process_update(photo(x.bot, 20, '/queue pricing'))
        await app.process_update(photo(x.bot, 21))
        await finish_timers(x.adapter)
        owner = waiting(x)[0]
        parts, _ = build_native_content_parts(owner.text, owner.media_urls)
        assert owner.text == 'pricing'
        assert sum(p['type'] == 'image_url' for p in parts) == 2
        assert len(waiting(x)) == len(x.bot.request.sent) == 1
        assert not x.adapter._seen_update_ids


@pytest.mark.asyncio
async def test_application_redispatch_cancelled_native_ack_does_not_rehold(replay):
    x = replay
    a = x.adapter
    entered = asyncio.Event()
    attempts = []
    request = x.bot.request
    send = request.do_request
    async def blocked(url, method, request_data=None, **kwargs):
        if url.endswith('/sendMessage'):
            attempts.append(dict(request_data.parameters))
            entered.set()
            await asyncio.Event().wait()
        return await send(url, method, request_data=request_data, **kwargs)
    request.do_request = blocked
    async with native_app(x) as app:
        a._drop_delayed_deliveries = True
        await app.process_update(photo(x.bot, 20, '/queue pricing', album=None))
        assert len(a._held_inbound_events) == 1
        a._drop_delayed_deliveries = False
        task = asyncio.create_task(a._redispatch_held_inbound())
        a._held_inbound_redispatch_task = task
        await asyncio.wait_for(entered.wait(), 2)
        a._drop_delayed_deliveries = True
        await a._cancel_pending_delivery_tasks()
        assert task.cancelled()
        assert not a._held_inbound_events
        assert [e.text for e in waiting(x)] == ['pricing']
        assert len(attempts) == 1 and not request.sent
        a._drop_delayed_deliveries = False
        await a._redispatch_held_inbound()
        assert len(waiting(x)) == 1 and len(attempts) == 1
        assert not a._seen_update_ids and not a._inflight_update_ids

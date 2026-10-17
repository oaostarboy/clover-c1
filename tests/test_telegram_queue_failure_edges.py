"""Admission acknowledgement and trust boundaries at each queue consumer."""
import asyncio
import dataclasses
from unittest.mock import AsyncMock

import pytest

from agent import workstream_origin as origin
from gateway.platforms.base import MessageType, merge_pending_message_event, MergeResult
from tests.test_telegram_album_queue_ownership import photo, replay  # noqa: F401
from tests.test_independent_telegram_album_review import finish_timers, waiting
from tests.test_telegram_queue_recovery_contract import redeem


@pytest.mark.asyncio
@pytest.mark.parametrize('album', ['pricing', None])
async def test_ordinary_photo_ack_error_never_falls_back_to_second_admission(replay, album):
    x = replay
    x.adapter._send_with_retry = AsyncMock(side_effect=RuntimeError('after admission ack error'))
    await x.adapter._handle_media_message(photo(x.bot, 20, 'pricing', album=album), None)
    await finish_timers(x.adapter)
    assert [e.text for e in waiting(x)] == ['pricing']
    assert [len(e.media_urls) for e in waiting(x)] == [1]
    assert not x.adapter._held_inbound_events


@pytest.mark.asyncio
async def test_queue_unavailable_is_not_an_accepted_handoff(replay):
    x = replay
    x.adapter._pending_messages = None
    event = x.adapter._build_message_event(photo(x.bot, 20).message, MessageType.PHOTO)
    event.text = '/queue pricing'
    assert await x.adapter.handle_message(event) is False
    assert not getattr(event, '_adapter_handoff_complete', False)
    assert x.adapter._send_with_retry.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['clover_plugin_id', 'clover_plugin_injection', 'gateway_session_key', 'gateway_session_id', 'gateway_session_strict'])
@pytest.mark.parametrize('route', ['shared', 'merge_or_enqueue', 'overflow_tail', 'queue'])
async def test_every_shared_merge_caller_respects_trust_metadata(replay, key, route):
    x = replay
    original = x.adapter._build_message_event(photo(x.bot, 20).message, MessageType.PHOTO)
    original.metadata = {'telegram_media_batch': 'album:owned', key: 'A'}
    original.media_urls = ['original.jpg']
    def incoming(value):
        return dataclasses.replace(original, metadata={**original.metadata, key: value}, media_urls=['incoming.jpg'])
    x.adapter._pending_messages[x.key] = original
    if route == 'overflow_tail':
        x.runner._session_state(x.key).conversation.queued_events.append(original)
    def merge(e):
        if route == 'shared':
            return merge_pending_message_event(x.adapter._pending_messages, x.key, e) is MergeResult.MERGED
        if route == 'merge_or_enqueue':
            return x.runner._merge_or_enqueue(x.adapter, x.key, e) is MergeResult.MERGED
        if route == 'overflow_tail':
            return x.runner._merge_into_overflow_tail(x.key, e)
        x.runner._queue_or_replace_pending_event(x.key, e)
        return len(original.media_urls) > 2
    # Positive control proves the route can merge before one independent mutation.
    same = incoming('A')
    if route == 'queue':
        assert x.runner._queue_or_replace_pending_event(x.key, same)
    else:
        assert merge(same)
    assert original.media_urls == ['original.jpg', 'incoming.jpg']
    merge(incoming('B'))
    assert original.media_urls == ['original.jpg', 'incoming.jpg']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['user', 'chat', 'thread', 'platform', 'actor', 'internal', 'synthetic', 'control', 'metadata', 'reply', 'text', 'source_text'])
async def test_queue_origin_transfer_refuses_changed_identity_without_consuming_source(replay, mutation):
    x = replay
    source = x.adapter._build_message_event(photo(x.bot, 90).message, MessageType.TEXT)
    source.text = '/queue pricing'
    assert origin.mint_human_ingress(source, platform='telegram', original_text=source.text)
    target = dataclasses.replace(source, text='pricing')
    if mutation in {'user', 'chat', 'thread'}:
        target.source = dataclasses.replace(source.source, **{mutation + '_id': 'different'})
    elif mutation == 'platform':
        from gateway.config import Platform
        target.source = dataclasses.replace(source.source, platform=Platform.DISCORD)
    elif mutation == 'actor':
        target.user_id = 'different'
    elif mutation == 'control':
        target.allow_gateway_control = False
    elif mutation == 'metadata':
        target.metadata = {'trusted_human_receipt': True}
    elif mutation == 'reply':
        target.reply_to_message_id = 'different'
    elif mutation == 'text':
        target.text = 'invented instruction'
    elif mutation == 'source_text':
        source.text = '/queue forged'
        target.text = 'forged'
    else:
        setattr(target, mutation, True)
    assert not origin.transfer_queued_human_ingress(source, target)
    assert redeem(target, x.key) is None
    proof = redeem(source, x.key)
    assert proof is not None
    origin.retire_human_ingress(proof)


@pytest.mark.asyncio
async def test_failed_redispatch_retains_remainder_without_hot_loop(replay):
    x = replay
    a = x.adapter
    a._drop_delayed_deliveries = True
    for number in [20, 30]:
        await a._handle_media_message(photo(x.bot, number, '/queue pricing', album=None), None)
    a._drop_delayed_deliveries = False
    a._message_handler = AsyncMock(side_effect=RuntimeError('not accepted'))
    await a._redispatch_held_inbound()
    assert len(a._held_inbound_events) == 2
    assert a._message_handler.await_count == 1
    assert not a._held_inbound_redispatch_task
    assert not waiting(x)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['platform', 'chat_type', 'actor'])
async def test_album_owner_does_not_cross_transport_or_actor(replay, mutation):
    from gateway.config import Platform
    x = replay
    original = x.adapter._build_message_event(photo(x.bot, 20).message, MessageType.PHOTO)
    original.metadata = {'telegram_media_batch': 'album:owned'}
    original.media_urls = ['original.jpg']
    x.adapter._pending_messages[x.key] = original
    same = dataclasses.replace(original, media_urls=['positive.jpg'])
    assert x.runner._merge_into_queued_media_batch(x.adapter, x.key, same)
    incoming = dataclasses.replace(original, media_urls=['incoming.jpg'])
    if mutation == 'actor':
        incoming.user_id = 'different-actor'
    else:
        change = {'platform': Platform.DISCORD} if mutation == 'platform' else {'chat_type': 'group'}
        incoming.source = dataclasses.replace(original.source, **change)
    assert not x.runner._merge_into_queued_media_batch(x.adapter, x.key, incoming)
    assert original.media_urls == ['original.jpg', 'positive.jpg']


@pytest.mark.asyncio
async def test_redispatch_post_handoff_error_holds_only_unprocessed_remainder(replay):
    x = replay
    a = x.adapter
    a._drop_delayed_deliveries = True
    for number in [20, 30]:
        await a._handle_media_message(photo(x.bot, number, '/queue pricing', album=None), None)
    handle = a.handle_message
    async def after_handoff(event):
        await handle(event)
        raise RuntimeError('transport wrapper after explicit acceptance')
    a.handle_message = after_handoff
    a._drop_delayed_deliveries = False
    await a._redispatch_held_inbound()
    assert len(waiting(x)) == 1
    assert [e.message_id for e in a._held_inbound_events] == ['30']
    a.handle_message = handle
    await a._redispatch_held_inbound()
    assert len(waiting(x)) == 2
    assert not a._held_inbound_events

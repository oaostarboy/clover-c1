"""Offline Bot API replay of album debounce crossing a /queue acknowledgement.

Uses real PTB Update/Message/PhotoSize/File and BasePlatformAdapter dispatch;
only the Bot API transport and the model boundary are offline.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from telegram import Bot, Update
from telegram.request import BaseRequest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.session import build_session_key
from plugins.platforms.telegram.adapter import TelegramAdapter


class OfflineTelegram(BaseRequest):
    def __init__(self):
        super().__init__()
        self.sent = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        if url.endswith('/getMe'):
            return 200, json.dumps({'ok': True, 'result': {
                'id': 123, 'is_bot': True, 'first_name': 'offline', 'username': 'offlinebot'}}).encode()
        if url.endswith('/sendMessage'):
            parameters = dict(request_data.parameters)
            self.sent.append(json.loads(json.dumps(parameters)))
            return 200, json.dumps({'ok': True, 'result': {
                'message_id': 100 + len(self.sent), 'date': 1792260817,
                'chat': {'id': parameters['chat_id'], 'type': 'private'},
                'text': parameters['text']}}).encode()
        if url.endswith('/getFile'):
            file_id = request_data.parameters['file_id']
            result = {'file_id': file_id, 'file_unique_id': file_id,
                      'file_size': 8, 'file_path': f'photos/{file_id}.jpg'}
            return 200, json.dumps({'ok': True, 'result': result}).encode()
        if '/file/bot' in url:
            import io
            from PIL import Image
            output = io.BytesIO()
            number = int(url.rsplit('/', 1)[-1].split('.')[0])
            Image.new('RGB', (2, 2), (number % 255, 0, 0)).save(output, format='JPEG')
            return 200, output.getvalue()
        raise AssertionError(f'unexpected offline API: {url.rsplit("/", 1)[-1]}')


def photo(bot, number, caption=None, album='pricing', user=7, chat=10, thread=None):
    msg = {'message_id': number, 'date': 1792260817,
           'chat': {'id': chat, 'type': 'private'},
           'from': {'id': user, 'is_bot': False, 'first_name': 'tester'},
           'photo': [{'file_id': str(number), 'file_unique_id': str(number),
                      'width': 100, 'height': 100}]}
    if caption:
        msg['caption'] = caption
    if album:
        msg['media_group_id'] = album
    if thread:
        msg['message_thread_id'] = thread
        msg['is_topic_message'] = True
    return Update.de_json({'update_id': number, 'message': msg}, bot)


@pytest_asyncio.fixture
async def replay(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner
    monkeypatch.setattr('gateway.platforms.base.IMAGE_CACHE_DIR', tmp_path / 'images')
    a = TelegramAdapter(PlatformConfig(enabled=True, token='123:offline'))
    a._is_user_authorized_from_message = lambda msg: True
    a._should_process_message = lambda msg: True
    a.MEDIA_GROUP_WAIT_SECONDS = .01
    a._media_batch_delay_seconds = .01
    r = object.__new__(GatewayRunner)
    r.config = GatewayConfig(platforms={Platform.TELEGRAM: a.config})
    r.adapters = {Platform.TELEGRAM: a}
    r._running_agents = {}
    r._running_agents_ts = {}
    r._pending_messages = {}
    r._queued_events = {}
    r._record_if_shutting_down = lambda event: None
    r._is_user_authorized = lambda source: True
    r._draining = False
    r._busy_input_mode = 'queue'
    r._busy_text_mode = 'interrupt'
    r._busy_ack_ts = {}
    r.session_store = None
    r.hooks = SimpleNamespace(emit=AsyncMock())
    a.set_busy_session_handler(r._handle_active_session_busy_message)
    r._bind_overflow_enqueuer(a)
    async def handler(event):
        key = build_session_key(event.source)
        if event.get_command() == 'queue':
            return await r._busy_queue_command(event, key, event.source)
        r._merge_or_enqueue(a, key, event)
        return None

    a._message_handler = handler
    a._send_with_retry = AsyncMock(return_value=SendResult(True, message_id='99'))
    bot = Bot('123:offline', request=OfflineTelegram())
    active = asyncio.create_task(asyncio.Event().wait())
    source = a._build_message_event(photo(bot, 1).message, MessageType.PHOTO).source
    key = build_session_key(source)
    a._active_sessions[key] = asyncio.Event()
    a._session_tasks[key] = active
    yield SimpleNamespace(adapter=a, runner=r, bot=bot, key=key, active=active)
    a._drop_delayed_deliveries = True
    await a._cancel_pending_delivery_tasks()
    active.cancel()
    await asyncio.gather(active, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_album_item_does_not_cancel_queue_ack_or_replay_caption(replay):
    a, r = replay.adapter, replay.runner
    # Exact incident shape: hero already owns the head, so pricing's first
    # acknowledgement is '(2 queued)', not an empty-queue confirmation.
    await a._handle_media_message(photo(replay.bot, 10, '/queue hero', album=None), None)
    await asyncio.sleep(.03)
    entered = asyncio.Event()
    release = asyncio.Event()
    sends = []

    async def send(**kwargs):
        sends.append(kwargs['content'])
        entered.set()
        await release.wait()
        return SendResult(True, message_id='99')

    a._send_with_retry = send
    await a._handle_media_message(photo(replay.bot, 20, '/queue pricing'), None)
    await asyncio.wait_for(entered.wait(), 1)
    await a._handle_media_message(photo(replay.bot, 21), None)
    await asyncio.sleep(.03)
    release.set()
    await asyncio.sleep(.03)
    assert sends == ['Queued for the next turn. (2 queued)']
    queued = [a._pending_messages[replay.key]] + list(r._session_state(replay.key).conversation.queued_events)
    assert [event.text for event in queued] == ['hero', 'pricing']
    assert [len(event.media_urls) for event in queued] == [1, 2]
    assert not replay.active.done()


@pytest.mark.asyncio
async def test_late_pricing_members_stay_with_pricing_not_earlier_hero(replay):
    a, r = replay.adapter, replay.runner
    await a._handle_media_message(photo(replay.bot, 10, '/queue hero', album=None), None)
    await asyncio.sleep(.03)
    hero = a._pending_messages[replay.key]
    await a._handle_media_message(photo(replay.bot, 20, '/queue pricing'), None)
    await asyncio.sleep(.03)
    pricing = r._session_state(replay.key).conversation.queued_events[0]
    await a._handle_media_message(photo(replay.bot, 21), None)
    await a._handle_media_message(photo(replay.bot, 22), None)
    await asyncio.sleep(.03)
    assert len(hero.media_urls) == 1
    assert len(pricing.media_urls) == 3
    assert [hero.text, pricing.text] == ['hero', 'pricing']
    assert a._send_with_retry.await_count == 2
    assert not replay.active.done()
    from agent.image_routing import build_native_content_parts
    contexts = [build_native_content_parts(event.text, event.media_urls)[0]
                for event in [hero, pricing]]
    assert [sum(part['type'] == 'image_url' for part in context)
            for context in contexts] == [1, 3]


@pytest.mark.asyncio
async def test_album_retries_keep_each_member_once_in_message_order(replay, monkeypatch):
    from plugins.platforms.telegram import adapter as module
    real_cache = module.cache_image_from_bytes
    cached = []

    def cache(data, **kwargs):
        path = real_cache(data, **kwargs)
        cached.append(path)
        return path

    monkeypatch.setattr(module, 'cache_image_from_bytes', cache)
    for number in [22, 20, 21, 21]:
        await replay.adapter._handle_media_message(
            photo(replay.bot, number, '/queue pricing' if number == 20 else None), None)
    await asyncio.sleep(.03)
    queued = replay.adapter._pending_messages[replay.key]
    assert queued.media_urls == [cached[1], cached[2], cached[0]]
    assert queued.media_types == ['image/jpg'] * 3
    assert queued.text == 'pricing'
    assert replay.adapter._send_with_retry.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('caption', ['/queue pricing', 'pricing'])
async def test_disconnect_during_ack_does_not_requeue_accepted_album(replay, caption):
    a, r = replay.adapter, replay.runner
    entered = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    a._send_with_retry = send
    await a._handle_media_message(photo(replay.bot, 20, caption), None)
    await asyncio.wait_for(entered.wait(), 1)
    a._drop_delayed_deliveries = True
    await a._cancel_pending_delivery_tasks()
    assert not a._held_inbound_events
    assert a._pending_messages[replay.key].text == 'pricing'
    assert not r._session_state(replay.key).conversation.queued_events
    assert not a._media_dispatch_tasks


@pytest.mark.asyncio
async def test_two_standalone_queue_photos_are_two_requests(replay):
    a = replay.adapter
    await a._handle_media_message(photo(replay.bot, 10, '/queue hero', album=None), None)
    await a._handle_media_message(photo(replay.bot, 20, '/queue pricing', album=None), None)
    await asyncio.sleep(.03)
    queued = [a._pending_messages[replay.key]] + list(
        replay.runner._session_state(replay.key).conversation.queued_events)
    assert [event.text for event in queued] == ['hero', 'pricing']
    assert [len(event.media_urls) for event in queued] == [1, 1]
    assert a._send_with_retry.await_count == 2


@pytest.mark.asyncio
async def test_cancel_before_queue_handoff_retains_same_caption_for_retry(replay):
    a = replay.adapter
    entered = asyncio.Event()
    handler = a._message_handler

    async def blocked(event):
        entered.set()
        await asyncio.Event().wait()

    a._message_handler = blocked
    await a._handle_media_message(photo(replay.bot, 20, '/queue pricing'), None)
    await asyncio.wait_for(entered.wait(), 1)
    a._drop_delayed_deliveries = True
    await a._cancel_pending_delivery_tasks()
    assert len(a._held_inbound_events) == 1
    held = a._held_inbound_events[0]
    assert held.text == '/queue pricing'
    assert len(held.media_urls) == 1
    a._message_handler = handler
    a._drop_delayed_deliveries = False
    await a._redispatch_held_inbound()
    assert not a._held_inbound_events
    assert a._pending_messages[replay.key].text == 'pricing'
    assert a._send_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_album_arrival_does_not_interrupt_active_task(replay):
    from unittest.mock import Mock
    r = replay.runner
    r._busy_input_mode = 'interrupt'
    r._session_has_compression_in_flight = AsyncMock(return_value=False)
    agent = SimpleNamespace(interrupt=Mock())
    r._running_agents[replay.key] = agent
    await replay.adapter._handle_media_message(photo(replay.bot, 20, 'ordinary album'), None)
    await asyncio.sleep(.03)
    agent.interrupt.assert_not_called()
    assert replay.adapter._pending_messages[replay.key].text == 'ordinary album'
    assert not replay.active.done()


@pytest.mark.asyncio
async def test_serialized_ack_and_model_context_have_one_owned_album(replay):
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import _dequeue_pending_event
    from agent.image_routing import build_native_content_parts
    a, r = replay.adapter, replay.runner
    await replay.bot.initialize()
    a._bot = replay.bot
    a._send_with_retry = BasePlatformAdapter._send_with_retry.__get__(a)
    await a._handle_media_message(photo(replay.bot, 20, '/queue pricing'), None)
    await asyncio.sleep(.03)
    await a._handle_media_message(photo(replay.bot, 21), None)
    await asyncio.sleep(.03)
    wire = replay.bot.request.sent
    assert len(wire) == 1
    assert wire[0]['text'] == r'Queued for the next turn\.'
    assert wire[0]['parse_mode'] == 'MarkdownV2'
    assert str(wire[0]['chat_id']) == '10'
    queued = _dequeue_pending_event(a, replay.key)
    assert queued is not None
    r._decide_image_input_mode = lambda **kwargs: 'native'
    text = await r._prepare_inbound_message_text(
        event=queued, source=queued.source, history=[], session_key=replay.key)
    paths = r._consume_pending_native_image_paths(replay.key)
    parts, skipped = build_native_content_parts(text, paths)
    serialized = json.loads(json.dumps({'role': 'user', 'content': parts}))
    assert not skipped
    assert [part['type'] for part in serialized['content']] == ['text', 'image_url', 'image_url']
    assert serialized['content'][0]['text'].startswith('pricing')
    assert '/queue' not in serialized['content'][0]['text']
    assert not a._pending_messages
    assert not r._session_state(replay.key).conversation.queued_events
    print(json.dumps({'serialized_ack_count': len(wire),
                      'dequeued_request_count': 1,
                      'model_image_count': sum(p['type'] == 'image_url' for p in serialized['content'])}))
    await replay.bot.shutdown()


@pytest.mark.asyncio
async def test_same_album_id_cannot_cross_sender_chat_or_topic(replay):
    a = replay.adapter
    a.handle_message = AsyncMock()
    for number, scope in enumerate([{}, {'user': 8}, {'chat': 11}, {'thread': 3}], start=30):
        await a._handle_media_message(photo(replay.bot, number, 'caption', **scope), None)
    await asyncio.sleep(.03)
    assert a.handle_message.await_count == 4
    events = [call.args[0] for call in a.handle_message.await_args_list]
    assert [len(event.media_urls) for event in events] == [1, 1, 1, 1]

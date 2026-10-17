"""The queued clone reaches the real AIAgent/provider and SessionDB boundaries."""
import asyncio
from types import SimpleNamespace

import pytest
from telegram import Bot, Update

from agent import workstream_origin as origin
from gateway.platforms.base import MessageType
from gateway.run import _dequeue_pending_event
from gateway.session import build_session_key
from tests.test_telegram_album_queue_ownership import OfflineTelegram, photo
from tests.test_telegram_queue_native_application import native_app
from tests.test_independent_telegram_album_review import finish_timers
from tests.gateway.test_trusted_human_origin_ingress import Harness, network_guard  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', ['text', 'album'])
async def test_queued_native_request_runs_real_agent_and_origin_lifecycle(monkeypatch, tmp_path, payload):
    # The remote model catalog is also an external network edge. Supply the
    # scripted provider's real fixture capability, not a fake image payload.
    monkeypatch.setattr('agent.models_dev.get_model_capabilities',
                        lambda *args, **kwargs: SimpleNamespace(supports_vision=True))
    h = Harness(monkeypatch, tmp_path, proactive=True)
    a, r = h.adapter, h.runner
    response_send = a.send
    a.send = type(a).send.__get__(a)
    bot = Bot('123:offline', request=OfflineTelegram())
    active = asyncio.create_task(asyncio.Event().wait())
    source = a._build_message_event(photo(bot, 1, chat=12345, user=12345).message, MessageType.PHOTO).source
    key = build_session_key(source)
    a._active_sessions[key] = asyncio.Event()
    a._session_tasks[key] = active
    a.MEDIA_GROUP_WAIT_SECONDS = .01
    r._bind_overflow_enqueuer(a)
    originals = []
    async def queue(event):
        originals.append(event)
        return await r._busy_queue_command(event, key, event.source)
    a.set_message_handler(queue)
    x = SimpleNamespace(adapter=a, runner=r, bot=bot)
    try:
        async with native_app(x) as app:
            update = photo(bot, 20, '/queue pricing', chat=12345, user=12345)
            if payload == 'text':
                raw = update.to_dict()
                raw['message'].pop('photo')
                raw['message'].pop('media_group_id')
                raw['message'].pop('caption')
                raw['message']['text'] = '/queue pricing'
                await app.process_update(Update.de_json(raw, bot))
                await asyncio.wait_for(asyncio.gather(*a._pending_text_batch_tasks.values()), 2)
            else:
                await app.process_update(update)
                await app.process_update(photo(bot, 21, chat=12345, user=12345))
                await finish_timers(a)
            queued = _dequeue_pending_event(a, key)
            assert queued is not None and queued.text == 'pricing'
            assert len(bot.request.sent) == 1
            a.send = response_send
            a._active_sessions.pop(key)
            a._session_tasks.pop(key)
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            r._decide_image_input_mode = lambda **kwargs: 'native'
            # No fake model/persistence boundary: the harness scripts only the
            # external provider client's response and auxiliary network edges.
            await r._handle_message(queued)
            assert h.scripted.calls == 1
            requests = h.scripted.requests
            user_parts = [m['content'] for m in requests[0]['messages'] if m['role'] == 'user']
            assert len(user_parts) == 1
            if payload == 'album':
                assert sum(p['type'] == 'image_url' for p in user_parts[0]) == 2
            else:
                assert user_parts[0] == 'pricing'
            rows = h.user_rows()
            assert len(rows) == 1
            receipt = h.db.get_human_origin(h.session_id(), rows[0]['id'])
            # Telegram's admitted text path mints; photos do not currently mint.
            assert (receipt is not None) == (payload == 'text')
            if receipt is not None:
                assert receipt.profile == str(h.home.resolve())
                assert h.db.consume_human_origin(h.session_id(), rows[0]['id'], consumer='test')
                assert h.db.get_human_origin(h.session_id(), rows[0]['id']) is None
            for event in [originals[0], queued]:
                assert origin.redeem_human_ingress(
                    event, platform='telegram', profile=str(h.home.resolve()),
                    session_id=h.session_id(), session_key=key, run_generation=1,
                    is_current=lambda: True,
                ) is None
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
        a._drop_delayed_deliveries = True
        await a._cancel_pending_delivery_tasks()
        h.close()

"""Real gateway/agent/consumer ownership; native SDK synthetic HTTP only."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from cron.presence import ThoughtStore
from gateway.platforms.base import SendResult


def enable_native_stream(monkeypatch, h, text='ack', preview_gate=None):
    from run_agent import AIAgent
    h.runner.config.streaming.enabled = True
    h.runner.config.streaming.transport = 'edit'
    h.runner.config.streaming.edit_interval = .01
    h.runner.config.streaming.buffer_threshold = 1
    h.adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id='out-1'))
    wires = []
    def transport(request):
        wire = json.loads(request.content)
        wires.append(wire)
        if wire.get('stream'):
            events = [dict(id='synthetic', object='chat.completion.chunk', created=0, model='synthetic',
                choices=[dict(index=0, delta={'content': text}, finish_reason=None)]),
                dict(id='synthetic', object='chat.completion.chunk', created=0, model='synthetic',
                choices=[dict(index=0, delta={}, finish_reason='stop')])]
            if preview_gate is not None:
                class DelayedStream(httpx.SyncByteStream):
                    def __iter__(self):
                        yield ('data: '+json.dumps(events[0])+'\n\n').encode()
                        assert preview_gate.wait(3), 'preview transport did not acknowledge'
                        yield ('data: '+json.dumps(events[1])+'\n\ndata: [DONE]\n\n').encode()
                return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=DelayedStream())
            return httpx.Response(200, headers={'content-type':'text/event-stream'},
                content=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n')
        return httpx.Response(200, json=dict(id='synthetic',object='chat.completion',created=0,model='synthetic',
            choices=[dict(index=0, finish_reason='stop',message={'role':'assistant','content':text})]))
    client = openai.OpenAI(api_key='synthetic', base_url='https://synthetic.invalid/v1',
        http_client=httpx.Client(transport=httpx.MockTransport(transport)), max_retries=0)
    monkeypatch.setattr(AIAgent, '_create_openai_client', lambda *a, **kw: client)
    return wires, client


def dispatch(h, text='Help verify deployment packaging', message_id=101):
    async def run():
        expected = len(h.results)+1
        await h.adapter._handle_text_message(h.update(text, message_id=message_id), object())
        deadline = asyncio.get_running_loop().time()+10
        while len(h.results)<expected or h.adapter._active_sessions:
            assert asyncio.get_running_loop().time()<deadline, 'requested reply did not settle'
            await asyncio.sleep(.01)
        await asyncio.sleep(.05)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['edit', 'draft', 'fresh', 'multipart'])
def test_actual_stream_final_ack_reaches_existing_after_reply_owner(monkeypatch, tmp_path, mode):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        text = ('Complete deployment evidence. '*350 if mode == 'multipart' else
                'The requested deployment verification is complete.' if mode in {'fresh', 'draft'} else 'ack')
        import threading
        gate = threading.Event() if mode in {'draft', 'fresh'} else None
        wires, client = enable_native_stream(monkeypatch, h, text, preview_gate=gate)
        if mode == 'fresh':
            monkeypatch.setattr(h.adapter, 'prefers_fresh_final_streaming', lambda *a, **kw: True)
            h.adapter.delete_message = AsyncMock(return_value=True)
        if mode == 'draft':
            h.runner.config.streaming.transport = 'draft'
            monkeypatch.setattr(h.adapter, 'supports_draft_streaming', lambda *a, **kw: True)
            async def draft_ack(*a, **kw):
                gate.set()
                return SendResult(success=True)
            h.adapter.send_draft = AsyncMock(side_effect=draft_ack)
        sent = []
        visible = {}
        async def acknowledge(*args, **kwargs):
            sent.append(kwargs.get('content', args[1] if len(args)>1 else ''))
            mid = str(len(sent))
            visible[mid] = sent[-1]
            if mode == 'fresh':
                gate.set()
            return SendResult(success=True, message_id=mid)
        async def edit(**kwargs):
            visible[str(kwargs['message_id'])] = kwargs['content']
            return SendResult(success=True, message_id=kwargs['message_id'])
        h.adapter.send.side_effect = acknowledge
        h.adapter.edit_message.side_effect = edit
        try:
            before = h.adapter.send.await_count
            dispatch(h)
            assert wires and wires[0]['stream'] is True
            assert h.results[-1]['already_sent'] is True
            if mode == 'multipart':
                assert len(sent)>1
                import re
                displayed = re.sub(r' \(\d+/\d+\)', '', ' '.join(visible.values()))
                assert ' '.join(displayed.split()) == ' '.join(text.split()), 'missing or duplicate multipart text'
            elif mode == 'fresh':
                assert h.adapter.send.await_count == before+2
                assert h.adapter.delete_message.await_count==1
                assert sent[-1]==text
            else:
                assert h.adapter.send.await_count == before+1, 'duplicate requested reply'
                if mode == 'draft':
                    assert h.adapter.send_draft.await_count>0
            assert len(requests)==1, 'actual stream final ACK did not admit presence'
            assert bot.send_message.await_count==1
            assert [p['state'] for p in ThoughtStore().publications()]==['acknowledged']
        finally:
            client.close()
    finally:
        h.close()


@pytest.mark.parametrize('race', ['bot', 'credential', 'adapter', 'scope', 'input', 'stop', 'interrupted', 'partial'])
def test_stream_ack_cannot_cross_turn_owner_or_revocation(monkeypatch, tmp_path, race):
    from gateway.config import Platform
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        wires, client = enable_native_stream(monkeypatch, h)
        original = h.runner._run_agent
        async def change_after_ack(*args, **kwargs):
            result = await original(*args, **kwargs)
            assert result.get('_requested_reply_receipt') is not None
            if race == 'bot':
                h.adapter._bot = SimpleNamespace(send_message=AsyncMock())
            elif race == 'credential':
                h.adapter.config.token = 'synthetic-rotated'
            elif race == 'adapter':
                h.runner.adapters[Platform.TELEGRAM] = SimpleNamespace(_bot=bot)
            elif race == 'scope':
                kwargs['source'].user_id = 'different-user'
            elif race == 'input':
                h.runner._invalidate_session_run_generation(kwargs['session_key'], reason='new-input')
            elif race == 'stop':
                ThoughtStore().stop()
            else:
                result[race] = True
            return result
        h.runner._run_agent = change_after_ack
        try:
            dispatch(h)
            assert wires
            assert not requests and bot.send_message.await_count==0
            assert not ThoughtStore().publications()
        finally:
            client.close()
    finally:
        h.close()


def test_already_sent_without_transport_receipt_does_not_admit_presence(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        original = h.runner._run_agent
        async def flags_only(*args, **kwargs):
            result = await original(*args, **kwargs)
            result['already_sent'] = True
            return result
        h.runner._run_agent = flags_only
        before = h.adapter.send.await_count
        dispatch(h)
        assert h.adapter.send.await_count==before, 'already_sent must not duplicate the requested reply'
        assert not requests and not ThoughtStore().publications()
        assert bot.send_message.await_count==0
    finally:
        h.close()


@pytest.mark.parametrize('mode', ['fresh', 'overflow', 'unknown', 'cancel', 'preview', 'commentary', 'optimistic'])
def test_consumer_requires_terminal_ack_not_flags_or_cleanup(monkeypatch, tmp_path, mode):
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    from gateway.requested_reply_delivery import RequestedReplyOwner
    from tests.gateway.test_trusted_human_origin_ingress import _user_source, SESSION_KEY
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        source = _user_source()
        generation = h.runner._begin_session_run_generation(SESSION_KEY)
        owner = RequestedReplyOwner.capture(h.runner, source, SESSION_KEY, generation)
        text = 'Complete deployment response.'
        config = StreamConsumerConfig(transport='edit', cursor='', fresh_final_after_seconds=.001)
        c = GatewayStreamConsumer(h.adapter, source.chat_id, config,
            run_still_current=lambda: h.runner._is_session_run_current(SESSION_KEY, generation))
        h.adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id='out-1'))
        h.adapter.delete_message = AsyncMock(return_value=True)
        async def unique_ack(*a, **kw):
            return SendResult(success=True, message_id=str(h.adapter.send.await_count))
        h.adapter.send.side_effect = unique_ack
        async def probe():
            if mode in {'fresh', 'unknown', 'preview', 'cancel'}:
                assert await c._send_or_edit(text)
                c._accumulated = text
                c._stream_ledger = text
                import time
                c._message_created_ts = time.monotonic()-10
            if mode == 'fresh':
                monkeypatch.setattr(h.adapter, 'prefers_fresh_final_streaming', lambda *a, **kw: True)
                c.finish(final_text=text)
                await c.run()
                assert h.adapter.send.await_count==2, 'fresh final did not use a new message'
                assert h.adapter.delete_message.await_count==1
            elif mode == 'overflow':
                text_long = 'Chunk delivery. '*700
                c.on_delta(text_long)
                c.finish(final_text=text_long)
                await c.run()
                assert h.adapter.send.await_count>1
                return owner.receipt(c, text_long)
            elif mode == 'unknown':
                h.adapter.send.side_effect = TimeoutError('synthetic ACK lost')
                c._fallback_prefix = text
                c._fallback_final_send = True
                await c._send_fallback_final(text)
                assert c.final_content_delivered is True, 'unknown ACK must retain no-replay suppression'
                assert h.adapter.send.await_count==2, 'unknown send was replayed'
            elif mode == 'cancel':
                task = asyncio.create_task(c.run())
                await asyncio.sleep(.01)
                task.cancel()
                await task
            elif mode == 'commentary':
                await c._send_commentary(text)
                c._final_response_sent = c._final_content_delivered = True
                c._record_turn_final_payload(text)
            elif mode == 'optimistic':
                c._final_response_sent = c._final_content_delivered = True
                c._record_turn_final_payload(text)
            return owner.receipt(c, text)
        receipt = asyncio.run(probe())
        assert (receipt is not None) == (mode in {'fresh', 'overflow'})
        if receipt is not None:
            assert receipt.transport_acks
            if mode == 'overflow':
                assert len(receipt.transport_acks)>1, 'sealed multipart head receipts were discarded'
            event = SimpleNamespace(source=source, internal=False)
            assert receipt.accepts(h.runner, event, SESSION_KEY, generation, receipt.text)
            assert not receipt.accepts(h.runner, event, SESSION_KEY, generation, 'footer only')
        assert not requests and bot.send_message.await_count==0
    finally:
        h.close()


"""Complete surviving-part coverage, independent of no-replay bookkeeping."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from gateway.platforms.base import SendResult
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from gateway.requested_reply_delivery import RequestedReplyOwner
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard, _user_source, SESSION_KEY  # noqa: F401


@pytest.mark.parametrize('fault', ['none', 'head', 'body', 'tail', 'duplicate_body', 'duplicate_identity', 'false_success', 'bookkeeping_throw', 'split_storage_raises'])
def test_complete_multipart_proof_is_separate_from_delivery(monkeypatch, tmp_path, fault):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        source = _user_source()
        generation = h.runner._begin_session_run_generation(SESSION_KEY)
        owner = RequestedReplyOwner.capture(h.runner, source, SESSION_KEY, generation)
        c = GatewayStreamConsumer(h.adapter, source.chat_id, StreamConsumerConfig(transport='edit', cursor=''),
            run_still_current=lambda: h.runner._is_session_run_current(SESSION_KEY, generation))
        sent = []
        async def send(*args, **kwargs):
            sent.append(kwargs['content'])
            return SendResult(success=1 if fault=='false_success' and len(sent)==2 else True, message_id=str(len(sent)))
        h.adapter.send.side_effect = send
        if fault == 'bookkeeping_throw':
            original = c._record_transport_ack
            def record(operation, content, result, *, terminal=True):
                if str(result.message_id)=='2':
                    raise LookupError('optional receipt storage fault')
                return original(operation, content, result, terminal=terminal)
            monkeypatch.setattr(c, '_record_transport_ack', record)
        if fault=='split_storage_raises':
            class UnavailableSplitStorage(list):
                def append(self, item):
                    raise LookupError('optional split provenance storage unavailable')
            c._final_receipt_splits = UnavailableSplitStorage()
        text = 'Requested complete body with distinct evidence. '*350
        c.on_delta(text); c.finish(final_text=text)
        asyncio.run(c.run())
        assert len(sent)>=3
        if fault in {'head', 'body', 'tail'}:
            del c._final_transport_acks[{'head':0, 'body':1, 'tail':-1}[fault]]
        elif fault=='duplicate_body':
            c._final_transport_acks[1] = c._final_transport_acks[0]
        elif fault=='duplicate_identity':
            c._final_transport_acks[1] = replace(c._final_transport_acks[1], message_ids=c._final_transport_acks[0].message_ids)
        assert c.delivered_final_matches(text) is True, 'proof loss must not change no-replay reconciliation'
        receipt = owner.receipt(c, text)
        assert (receipt is not None)==(fault=='none')
        assert not requests and not bot.send_message.await_count
    finally:
        h.close()


@pytest.mark.parametrize('change', ['delete_body', 'replace_body', 'full_replacement', 'duplicate_tail'])
def test_surviving_parts_exclude_deleted_or_superseded_evidence(monkeypatch, tmp_path, change):
    h, _, _ = setup_host(monkeypatch, tmp_path)
    try:
        source = _user_source()
        generation = h.runner._begin_session_run_generation(SESSION_KEY)
        owner = RequestedReplyOwner.capture(h.runner, source, SESSION_KEY, generation)
        c = GatewayStreamConsumer(h.adapter, source.chat_id, StreamConsumerConfig(transport='edit', cursor=''))
        serial = 0
        async def send(*args, **kwargs):
            nonlocal serial
            serial += 1
            return SendResult(success=True, message_id=str(serial))
        async def edit(**kwargs):
            return SendResult(success=True, message_id=kwargs['message_id'])
        h.adapter.send.side_effect = send
        h.adapter.edit_message = AsyncMock(side_effect=edit)
        h.adapter.delete_message = AsyncMock(return_value=True)
        text = 'Distinct requested deployment evidence. '*350
        async def run():
            c.on_delta(text); c.finish(final_text=text); await c.run()
            assert owner.receipt(c, text) is not None
            if change=='delete_body':
                await c._io.delete_message(source.chat_id, '2')
            elif change=='replace_body':
                c._message_id = '2'
                await c._send_or_edit('Not the requested body', finalize=True, is_turn_final=False)
            elif change=='full_replacement':
                c._message_id = None
                await c._send_or_edit(text.strip(), finalize=True)
            else:
                c._final_transport_acks.append(c._final_transport_acks[-1])
        asyncio.run(run())
        result = owner.receipt(c, text)
        assert (result is not None)==(change in {'full_replacement', 'duplicate_tail'})
        if result is not None:
            ids = [mid for a in result.transport_acks for mid in a.message_ids]
            assert len(ids)==len(set(ids)), 'physical parts double counted'
            if change=='full_replacement':
                assert len(result.transport_acks)==1
    finally:
        h.close()


@pytest.mark.parametrize('payload', ['prose', 'code'])
def test_multiple_progressive_heads_keep_exact_split_provenance(monkeypatch, tmp_path, payload):
    h, _, _ = setup_host(monkeypatch, tmp_path)
    try:
        source = _user_source()
        generation = h.runner._begin_session_run_generation(SESSION_KEY)
        owner = RequestedReplyOwner.capture(h.runner, source, SESSION_KEY, generation)
        c = GatewayStreamConsumer(h.adapter, source.chat_id,
            StreamConsumerConfig(transport='edit', cursor='', buffer_threshold=1, edit_interval=.001))
        async def run():
            preview = asyncio.Event()
            serial = 0
            async def send(*args, **kwargs):
                nonlocal serial
                serial += 1
                preview.set()
                return SendResult(success=True, message_id=str(serial))
            async def edit(**kwargs):
                return SendResult(success=True, message_id=kwargs['message_id'])
            h.adapter.send.side_effect = send
            h.adapter.edit_message = AsyncMock(side_effect=edit)
            prefix = 'Deployment preview.\n'
            body = 'Verifiable evidence across progressive heads.\n'*350
            text = prefix+body if payload=='prose' else prefix+'```python\n'+body+'```\n'
            c.on_delta(prefix)
            task = asyncio.create_task(c.run())
            await asyncio.wait_for(preview.wait(), 2)
            c.on_delta(text[len(prefix):]); c.finish(final_text=text)
            await asyncio.wait_for(task, 3)
            return text
        text = asyncio.run(run())
        receipt = owner.receipt(c, text)
        assert receipt is not None
        assert len(receipt.transport_acks)>2
        if payload=='code':
            from gateway.stream_consumer import ensure_closed_code_fences
            assert all(a.content==ensure_closed_code_fences(a.content) for a in receipt.transport_acks), 'receipt text must be the actual balanced outbound payload'
        assert receipt.transport_acks[0].operation=='edit_message'
        assert not receipt.transport_acks[0].terminal
    finally:
        h.close()

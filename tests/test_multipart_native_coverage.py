"""Native PTB proof loss and progressive edited heads; synthetic HTTP only."""
import re
import pytest
from cron.presence import ThoughtStore
from gateway.stream_consumer import GatewayStreamConsumer
from gateway.requested_reply_delivery import RequestedReplyOwner
from tests.test_presence_stream_native_wire import native_telegram_stream
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


@pytest.mark.parametrize('fault', ['none', 'head', 'body', 'tail', 'storage_raises', 'progressive'])
def test_native_complete_surviving_parts(monkeypatch, tmp_path, fault):
    receipts = []
    diagnostics = []
    dropped = []
    head_number = 0
    record = GatewayStreamConsumer._record_transport_ack
    def recording(self, operation, content, result, *, terminal=True):
        nonlocal head_number
        if not terminal:
            head_number += 1
        if ((fault=='head' and not terminal and head_number==1)
                or (fault=='body' and not terminal and head_number==2)
                or (fault=='tail' and terminal)
                or (fault=='storage_raises' and not terminal)):
            assert result.success is True
            dropped.append(content)
            if fault=='storage_raises':
                raise LookupError('optional native receipt storage failed after ACK')
            return
        return record(self, operation, content, result, terminal=terminal)
    monkeypatch.setattr(GatewayStreamConsumer, '_record_transport_ack', recording)
    receipt = RequestedReplyOwner.receipt
    def observe(self, consumer, final_text):
        result = receipt(self, consumer, final_text)
        if consumer is not None:
            receipts.append(result)
            diagnostics.append(dict(target=(len(final_text), repr(final_text[:30]), repr(final_text[-30:])),
                splits=[(len(s), [(len(c), repr(c[:30]), repr(c[-30:])) for c in chunks]) for s,chunks in getattr(consumer, '_final_receipt_splits', [])],
                acks=[(a.operation,a.message_ids,len(a.content),repr(a.content[:30]),repr(a.content[-30:]),a.terminal) for a in consumer._final_transport_acks],
                sent=consumer._final_response_sent, delivered=len(consumer._delivered_final_text or '')))
        return result
    monkeypatch.setattr(RequestedReplyOwner, 'receipt', observe)
    with native_telegram_stream(monkeypatch, tmp_path, 'multipart', progressive=fault=='progressive') as observed:
        positive = fault in {'none', 'progressive'}
        assessments = len(observed.requests)
        assert assessments==int(positive), diagnostics
        assert [p['state'] for p in ThoughtStore().publications()]==(['acknowledged'] if positive else [])
        assert any(receipts)==positive
        if fault=='progressive':
            assert any(a.operation=='edit_message' and not a.terminal
                for r in receipts if r for a in r.transport_acks), 'sealed native edit missing proof'
        if not positive:
            assert dropped
        visible = {}
        sends = []
        for index, (method, params) in enumerate(observed.endpoint.calls, 1):
            if method=='sendMessage':
                visible[str(index+100)] = params['text']
                sends.append(params['text'])
            elif method=='editMessageText':
                visible[str(params['message_id'])] = params['text']
            elif method=='deleteMessage':
                visible.pop(str(params['message_id']), None)
        initiatives = [text for text in visible.values() if 'Deployment packaging' in text]
        assert len(initiatives)==int(positive)
        requested = [text for text in visible.values() if 'Deployment packaging' not in text]
        parts = [re.sub(r' \\\(\d+/\d+\\\)', '', text) for text in requested]
        # Progressive overflow cuts at a codepoint budget, sometimes inside a
        # word; the canonical no-preview splitter cuts between words.
        displayed = ('' if fault=='progressive' else ' ').join(parts)
        assert displayed.split()==observed.text.split(), 'ordinary reply dropped or replayed'
        assert len(sends)==3+int(positive), 'ordinary native reply sent more than once'


@pytest.mark.parametrize('race', ['bot', 'credential', 'adapter', 'scope', 'input', 'interrupted'])
def test_native_complete_proof_cannot_cross_its_owner(monkeypatch, tmp_path, race):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway.config import Platform
    def configure(h):
        original = h.runner._run_agent
        async def revoke_after_ack(*args, **kwargs):
            result = await original(*args, **kwargs)
            assert result.get('_requested_reply_receipt') is not None
            if race=='bot':
                h.adapter._bot = SimpleNamespace(send_message=AsyncMock())
            elif race=='credential':
                h.adapter.config.token = 'synthetic-rotated'
            elif race=='adapter':
                h.runner.adapters[Platform.TELEGRAM] = SimpleNamespace(_bot=h.adapter._bot)
            elif race=='scope':
                kwargs['source'].user_id = 'different-user'
            elif race=='input':
                h.runner._invalidate_session_run_generation(kwargs['session_key'], reason='new-input')
            else:
                result['interrupted'] = True
            return result
        h.runner._run_agent = revoke_after_ack
    with native_telegram_stream(monkeypatch, tmp_path, 'multipart', configure=configure) as observed:
        assert not observed.requests and not ThoughtStore().publications()
        sends = [p['text'] for method,p in observed.endpoint.calls if method=='sendMessage']
        assert len(sends)==3, 'owner revocation must not replay the ordinary reply'
        assert not any('Deployment packaging' in text for text in sends)
        visible = {}
        for index, (method, params) in enumerate(observed.endpoint.calls, 1):
            if method=='sendMessage':
                visible[str(index+100)] = params['text']
            elif method=='editMessageText':
                visible[str(params['message_id'])] = params['text']
        displayed = ' '.join(re.sub(r' \\\(\d+/\d+\\\)', '', text) for text in visible.values())
        assert displayed.split()==observed.text.split()

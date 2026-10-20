"""Optional receipt bookkeeping must not reinterpret a successful platform ACK."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from gateway.requested_reply_delivery import RequestedReplyOwner, RequestedReplyReceipt
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_presence_stream_receipts import enable_native_stream, dispatch
from tests.gateway.test_trusted_human_origin_ingress import network_guard, _user_source  # noqa: F401


def unavailable(*args, **kwargs):
    raise LookupError('synthetic optional fence unavailable')


@pytest.mark.parametrize('operation', ['final', 'sealed_head'])
def test_post_ack_fence_error_never_reclassifies_successful_transport(monkeypatch, tmp_path, operation):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        c = GatewayStreamConsumer(h.adapter, '12345', StreamConsumerConfig(transport='edit'),
                                  run_still_current=unavailable)
        async def run():
            if operation=='final':
                return await c._send_or_edit('Complete requested response', finalize=True)
            return await c._send_new_chunk('Sealed requested head', None, final=True)
        result = asyncio.run(run())
        assert result == (True if operation=='final' else 'out-1'), 'a successful ACK became a replayable failure'
        assert h.adapter.send.await_count==1
        assert not c._final_transport_acks, 'unverifiable optional receipt must hold'
        assert not requests and bot.send_message.await_count==0
    finally:
        h.close()


def test_optional_owner_capture_error_holds_without_raising(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(h.runner, '_adapter_credential_fingerprint', unavailable)
        assert RequestedReplyOwner.capture(h.runner, _user_source(), 'session', 1) is None
        assert not requests and bot.send_message.await_count==0
    finally:
        h.close()


def test_receipt_acceptance_error_keeps_already_streamed_requested_reply(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        _, client = enable_native_stream(monkeypatch, h)
        monkeypatch.setattr(RequestedReplyReceipt, 'accepts', unavailable)
        try:
            before = h.adapter.send.await_count
            dispatch(h)
            assert h.adapter.send.await_count==before+1, 'optional receipt fault duplicated the requested reply'
            assert not requests and bot.send_message.await_count==0
        finally:
            client.close()
    finally:
        h.close()


@pytest.mark.parametrize('outcome', ['ack', 'failed', 'unknown', 'truthy', 'foreign'])
def test_reconciliation_requires_literal_ack_and_exact_owner(monkeypatch, tmp_path, outcome):
    from types import SimpleNamespace
    from gateway.platforms.base import SendResult
    from tests.gateway.test_trusted_human_origin_ingress import SESSION_KEY
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        source = _user_source()
        generation = h.runner._begin_session_run_generation(SESSION_KEY)
        owner = RequestedReplyOwner.capture(h.runner, source, SESSION_KEY, generation)
        result = SendResult(success=outcome in {'ack', 'foreign'}, message_id='final',
                            continuation_message_ids=('head', 'tail'))
        if outcome == 'unknown':
            result = SimpleNamespace(message_id='unknown')
        elif outcome == 'truthy':
            result = SimpleNamespace(success=1, message_id='not-a-literal-ack')
        adapter = object() if outcome == 'foreign' else h.adapter
        receipt = owner.receipt_for_edit(adapter, result, 'Complete final with footer')
        assert (receipt is not None) == (outcome=='ack')
        if receipt is not None:
            assert receipt.transport_acks[0].content=='Complete final with footer'
            assert receipt.transport_acks[0].message_ids==('final', 'head', 'tail')
            event = SimpleNamespace(source=source, internal=False)
            assert receipt.accepts(h.runner, event, SESSION_KEY, generation, receipt.text)
            assert not receipt.accepts(h.runner, event, SESSION_KEY, generation, 'footer')
            source.thread_id = 'foreign-thread'
            assert not receipt.accepts(h.runner, event, SESSION_KEY, generation, receipt.text)
        assert not requests and bot.send_message.await_count==0
    finally:
        h.close()

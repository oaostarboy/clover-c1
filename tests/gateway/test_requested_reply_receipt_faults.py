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

"""Real PTB exception hierarchy; fake Bot boundary, no Telegram calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import NetworkError

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize('message', ['lost remote acknowledgement', 'could not parse remote response'])
async def test_remote_accept_then_read_failure_does_not_resend(monkeypatch, message):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic-token'))
    accepted = []
    async def send_message(**kw):
        accepted.append(kw['text'])
        if len(accepted) == 1:
            try:
                raise httpx.ReadError('peer reset after accepting request')
            except httpx.ReadError as cause:
                raise NetworkError('httpx.ReadError: ' + message) from cause
        return SimpleNamespace(message_id=7)
    adapter._bot = SimpleNamespace(send_message=send_message)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    result = await adapter.send('123', 'one pending reply', metadata={'notify': True})
    assert len(accepted) == 1, 'ambiguous acknowledged send was silently duplicated'
    assert not result.success and result.retryable is False
    assert result.raw_response['delivery_uncertain'] is True


@pytest.mark.asyncio
async def test_definite_connection_failure_keeps_bounded_retry(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic-token'))
    calls = []
    async def send_message(**kw):
        calls.append(kw['text'])
        if len(calls) < 3:
            try:
                raise httpx.ConnectError('connection refused before dispatch')
            except httpx.ConnectError as cause:
                raise NetworkError('httpx.ConnectError: connection refused') from cause
        return SimpleNamespace(message_id=7)
    adapter._bot = SimpleNamespace(send_message=send_message)
    sleeps = AsyncMock()
    monkeypatch.setattr(asyncio, 'sleep', sleeps)
    result = await adapter.send('123', 'one pending reply', metadata={'notify': True})
    assert result.success and calls == ['one pending reply'] * 3
    assert [call.args[0] for call in sleeps.call_args_list] == [1, 2]


@pytest.mark.asyncio
async def test_sent_first_chunk_is_not_replayed_after_unsent_second_chunk(monkeypatch):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic-token'))
    monkeypatch.setattr(adapter, 'format_message', lambda content: content)
    monkeypatch.setattr(adapter, 'truncate_message', lambda *args, **kwargs: ['chunk-one', 'chunk-two'])
    accepted, attempts = [], []
    async def send_message(**kw):
        attempts.append(kw['text'])
        if kw['text'] == 'chunk-two':
            try:
                raise httpx.ConnectError('connection refused before dispatch')
            except httpx.ConnectError as cause:
                raise NetworkError('httpx.ConnectError: connection refused') from cause
        accepted.append(kw['text'])
        return SimpleNamespace(message_id=7)
    adapter._bot = SimpleNamespace(send_message=send_message)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    result = await adapter.send('123', 'a long reply', metadata={'notify': True})
    assert accepted == ['chunk-one']
    assert attempts.count('chunk-two') == 3
    assert result.retryable is False, 'whole-response retry would duplicate the accepted first chunk'
    assert result.raw_response['message_ids'] == ['7']

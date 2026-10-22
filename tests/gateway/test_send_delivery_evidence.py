"""Whole-send replay safety at the shared consumer and real Bot/ledger seams."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import NetworkError

from gateway.platforms.base import SendResult, MessageEvent, MessageType
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_send_retry import _StubAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', [
    {'raw_response': {'delivery_uncertain': True}},
    {'raw_response': {'message_ids': ['accepted-1']}},
    {'message_id': 'accepted-1'},
    {'continuation_message_ids': ('accepted-2',)},
])
@pytest.mark.parametrize('after_retry', [False, True])
async def test_shared_wrapper_preserves_delivery_evidence_over_retry_and_fallback(monkeypatch, evidence, after_retry):
    adapter = _StubAdapter()
    uncertain = SendResult(success=False, error='ConnectionResetError: cannot parse reply',
                           retryable=True, **evidence)
    adapter._send_results = ([SendResult(success=False, error='ConnectError', retryable=True)]
                             if after_retry else []) + [uncertain]
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    result = await adapter._send_with_retry('123', 'original reply', max_retries=2)
    assert result is uncertain
    assert len(adapter._send_calls) == 1 + int(after_retry)
    assert sleep.await_count == int(after_retry)


@pytest.mark.asyncio
async def test_timeout_after_definite_unsent_retry_is_not_plain_fallback(monkeypatch):
    adapter = _StubAdapter()
    uncertain = SendResult(success=False, error='ReadTimeout: request timed out')
    adapter._send_results = [SendResult(success=False, error='ConnectError', retryable=True), uncertain]
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    assert await adapter._send_with_retry('123', 'original reply') is uncertain
    assert len(adapter._send_calls) == 2


@pytest.mark.asyncio
async def test_plain_fallback_uncertainty_is_returned_without_another_send(monkeypatch):
    adapter = _StubAdapter()
    uncertain = SendResult(success=False, error='ack lost', raw_response={'delivery_uncertain': True})
    adapter._send_results = [SendResult(success=False, error="Bad Request: can't parse entities"), uncertain]
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    assert await adapter._send_with_retry('123', 'original reply') is uncertain
    assert len(adapter._send_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('recover', [False, True])
async def test_real_bot_definite_unsent_to_accepted_transition_stops_at_uncertainty(monkeypatch, recover):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic-token'))
    attempts, accepted = [], []
    async def send_message(**kw):
        attempts.append(kw['text'])
        if len(attempts) <= 3:
            try:
                raise httpx.ConnectError('before dispatch')
            except httpx.ConnectError as cause:
                raise NetworkError('httpx.ConnectError: before dispatch') from cause
        accepted.append(kw['text'])
        if not recover:
            try:
                raise httpx.ReadError('ack lost')
            except httpx.ReadError as cause:
                raise NetworkError('ConnectionResetError: ack lost') from cause
        return SimpleNamespace(message_id=7)
    adapter._bot = SimpleNamespace(send_message=send_message)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    result = await adapter._send_with_retry('123', 'reply', metadata={'notify': True})
    assert len(attempts) == 4 and len(accepted) == 1
    assert result.success is recover
    if not recover:
        assert result.raw_response['delivery_uncertain'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('chunks', [False, True])
async def test_actual_final_send_retains_failed_obligation_without_runtime_replay(tmp_path, monkeypatch, chunks):
    from gateway import delivery_ledger as dl
    monkeypatch.setattr(dl, '_db_path', lambda: tmp_path / 'state.db')
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic-token'))
    if chunks:
        monkeypatch.setattr(adapter, 'format_message', lambda x: x)
        monkeypatch.setattr(adapter, 'truncate_message', lambda *a, **k: ['one', 'two'])
    accepted = []
    async def send_message(**kw):
        if chunks and kw['text'] == 'two':
            try:
                raise httpx.ConnectError('before dispatch')
            except httpx.ConnectError as cause:
                raise NetworkError('httpx.ConnectError: before dispatch') from cause
        accepted.append(kw['text'])
        if not chunks:
            try:
                raise httpx.ReadError('ack lost')
            except httpx.ReadError as cause:
                raise NetworkError('ConnectionResetError: ack lost') from cause
        return SimpleNamespace(message_id=7)
    adapter._bot = SimpleNamespace(send_message=send_message)
    adapter._message_handler = AsyncMock(return_value='final reply')
    adapter._keep_typing = AsyncMock()  # transport fixture has no typing Bot API
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    event = MessageEvent(text='hello', message_type=MessageType.TEXT,
                         source=SessionSource(platform=Platform.TELEGRAM, chat_id='123', chat_type='dm'),
                         message_id='42')
    key = 'agent:main:telegram:dm:123'
    adapter._active_sessions[key] = asyncio.Event()
    await adapter._process_message_background(event, key)
    assert len(accepted) == 1
    with dl._connect() as conn:
        rows = conn.execute('SELECT content, state FROM delivery_obligations').fetchall()
    assert rows == [('final reply', 'failed')]
    assert dl.sweep_failed_for_runtime('telegram') == []

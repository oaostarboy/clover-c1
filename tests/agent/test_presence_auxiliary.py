"""Physical HTTP requests through the real async OpenAI SDK, offline."""
import asyncio
import json
import httpx
import openai
import pytest
from agent import auxiliary_client as aux


def test_custom_presence_requires_exact_endpoint_before_resolver(monkeypatch):
    calls = []
    monkeypatch.setattr(aux, 'resolve_provider_client', lambda *a, **k: (calls.append(k), None))
    monkeypatch.setattr('clover_cli.config.load_config', lambda **kw: {
        'auxiliary': {'presence': {'provider': 'custom', 'model': 'synthetic-model'}}})
    with pytest.raises(ValueError):
        asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'synthetic capsule'}],
                                             max_output_tokens=512, timeout=1))
    assert calls == [], 'missing custom route entered generic API-key fallback detection'


def test_presence_aux_native_timeout_cancels_single_dispatch(monkeypatch, record_property):
    from tests.agent.presence_deadline_helpers import DeadlineClock

    calls, cancelled = [], []

    async def scenario():
        # Keep cold SDK/setup inside the ordinary production budget. Expire the
        # original absolute deadline only AFTER the transport is in flight.
        clock = DeadlineClock(monkeypatch)
        dispatched = asyncio.Event()
        blocked = asyncio.Future()

        async def transport(request):
            calls.append(request)
            clock.mark('physical_dispatch')
            dispatched.set()
            try:
                await blocked
                raise AssertionError('in-flight block unexpectedly released')
            except asyncio.CancelledError:
                cancelled.append(True)
                clock.mark('transport_cancelled')
                raise

        client = openai.AsyncOpenAI(api_key='synthetic-key', base_url='https://api.openai.com/v1',
            max_retries=7, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        monkeypatch.setattr(aux, 'resolve_provider_client', lambda *a, **k: (client, 'synthetic-model'))
        monkeypatch.setattr('clover_cli.config.load_config', lambda **kw: {
            'auxiliary': {'presence': {'provider': 'openai', 'model': 'synthetic-model'}}})
        clock.mark('completion_started')
        task = asyncio.create_task(aux.presence_completion(
            messages=[{'role': 'user', 'content': 'synthetic capsule'}],
            max_output_tokens=512, timeout=5))
        ready = asyncio.create_task(dispatched.wait())
        try:
            await asyncio.wait({task, ready}, return_when=asyncio.FIRST_COMPLETED)
            assert dispatched.is_set(), 'deadline expired during setup, not in-flight cancellation'
            assert not task.done() and not blocked.done()
            clock.mark('inflight_block_observed')
            clock.advance(5)
            with pytest.raises(TimeoutError):
                await task
            clock.mark('completion_timed_out')
            assert len(calls) == 1 and cancelled == [True]
            assert blocked.cancelled() and client.is_closed()
            # Flush ready callbacks: a hidden retry must not appear afterwards.
            await asyncio.sleep(0)
            assert len(calls) == 1
        finally:
            record_property('deadline_readiness_trace', json.dumps(clock.events))
            for pending in (task, ready):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, ready, return_exceptions=True)
            await client.close()

    asyncio.run(scenario())


def test_presence_aux_has_one_physical_attempt_and_no_fallback(monkeypatch):
    assert hasattr(aux, 'presence_completion'), 'missing bounded auxiliary request path'
    calls = []
    async def transport(request):
        calls.append(request)
        return httpx.Response(429, json={'error': {'message': 'synthetic limit'}})
    client = openai.AsyncOpenAI(api_key='synthetic-key', base_url='https://api.openai.com/v1',
                                max_retries=7, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(aux, 'resolve_provider_client', lambda *a, **k: (client, 'synthetic-model'))
    monkeypatch.setattr('clover_cli.config.load_config', lambda **kw: {
        'auxiliary': {'presence': {'provider': 'openai', 'model': 'synthetic-model'}}})
    with pytest.raises(openai.RateLimitError):
        asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'synthetic capsule'}],
                                             max_output_tokens=512, timeout=1))
    assert len(calls) == 1


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_presence_aux_never_follows_native_http_redirect(monkeypatch, status):
    calls = []
    async def transport(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, headers={'location':'https://api.openai.com/v1/redirected'})
        return httpx.Response(200, json={'choices':[{'message':{'content':'second physical request'}}]})
    client = openai.AsyncOpenAI(api_key='synthetic-key', base_url='https://api.openai.com/v1',
        max_retries=7, http_client=httpx.AsyncClient(follow_redirects=True,
            transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(aux, 'resolve_provider_client', lambda *a, **k: (client, 'synthetic-model'))
    monkeypatch.setattr('clover_cli.config.load_config', lambda **kw: {
        'auxiliary': {'presence': {'provider': 'openai', 'model': 'synthetic-model'}}})
    with pytest.raises(openai.APIStatusError):
        asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'synthetic capsule'}],
            max_output_tokens=512, timeout=1))
    assert len(calls) == 1 and client.is_closed()

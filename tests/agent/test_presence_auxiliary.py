"""Physical HTTP requests through the real async OpenAI SDK, offline."""
import asyncio
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


def test_presence_aux_native_timeout_cancels_single_dispatch(monkeypatch):
    calls, cancelled = [], []
    async def transport(request):
        calls.append(request)
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    client = openai.AsyncOpenAI(api_key='synthetic-key', base_url='https://synthetic.invalid/v1',
        max_retries=7, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(aux, 'resolve_provider_client', lambda *a, **k: (client, 'synthetic-model'))
    monkeypatch.setattr('clover_cli.config.load_config', lambda **kw: {
        'auxiliary': {'presence': {'provider': 'openai', 'model': 'synthetic-model'}}})
    with pytest.raises(TimeoutError):
        asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'synthetic capsule'}],
                                             max_output_tokens=512, timeout=0.05))
    assert len(calls) == 1 and cancelled == [True]


def test_presence_aux_has_one_physical_attempt_and_no_fallback(monkeypatch):
    assert hasattr(aux, 'presence_completion'), 'missing bounded auxiliary request path'
    calls = []
    async def transport(request):
        calls.append(request)
        return httpx.Response(429, json={'error': {'message': 'synthetic limit'}})
    client = openai.AsyncOpenAI(api_key='synthetic-key', base_url='https://synthetic.invalid/v1',
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
            return httpx.Response(status, headers={'location':'https://synthetic.invalid/v1/redirected'})
        return httpx.Response(200, json={'choices':[{'message':{'content':'second physical request'}}]})
    client = openai.AsyncOpenAI(api_key='synthetic-key', base_url='https://synthetic.invalid/v1',
        max_retries=7, http_client=httpx.AsyncClient(follow_redirects=True,
            transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(aux, 'resolve_provider_client', lambda *a, **k: (client, 'synthetic-model'))
    monkeypatch.setattr('clover_cli.config.load_config', lambda **kw: {
        'auxiliary': {'presence': {'provider': 'openai', 'model': 'synthetic-model'}}})
    with pytest.raises(openai.APIStatusError):
        asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'synthetic capsule'}],
            max_output_tokens=512, timeout=1))
    assert len(calls) == 1 and client.is_closed()

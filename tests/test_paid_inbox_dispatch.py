"""Actual OpenAI SDK -> offline HTTP transport: never a provider endpoint."""
import json
import httpx
import pytest
from openai import OpenAI
from agent import auxiliary_client as aux


def test_fixed_watcher_route_has_one_physical_attempt_no_sdk_retry_or_fallback():
    dispatch = getattr(aux, 'call_bounded_inbox', None)
    assert callable(dispatch), 'watcher needs a physical-dispatch, no-fallback seam'
    calls, reservations = [], []
    def send(request):
        calls.append(json.loads(request.content))
        return httpx.Response(500, json={'error': {'message': 'synthetic failure'}})
    client = OpenAI(api_key='synthetic-only', base_url='https://offline.invalid/v1',
                    max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(send)))
    with pytest.raises(Exception):
        dispatch(client=client, messages=[{'role':'user','content':'test'}],
                 max_output_tokens=128, reserve=lambda: reservations.append('reserved'))
    assert len(calls) == len(reservations) == 1
    assert calls[0]['model'] == 'gpt-4o-mini-2024-07-18'
    assert calls[0]['max_tokens'] == 128 and 'tools' not in calls[0]
    client.max_retries = 2
    with pytest.raises(ValueError):
        dispatch(client=client, messages=[], max_output_tokens=128, reserve=lambda: None)
    assert len(calls) == 1


def test_sdk_default_redirect_client_is_not_an_approved_physical_route():
    client = OpenAI(api_key='synthetic', base_url='https://offline.invalid/v1', max_retries=0)
    with pytest.raises(ValueError):
        aux.call_bounded_inbox(client=client, messages=[], max_output_tokens=128,
                              reserve=lambda: (_ for _ in ()).throw(AssertionError('no reservation')))
    client.close()


def test_production_route_factory_disables_redirects_and_transport_retries():
    factory = getattr(aux, 'create_bounded_inbox_client', None)
    assert callable(factory), 'fixed bounded client construction is required'
    with factory('synthetic') as client:
        assert str(client.base_url) == 'https://api.openai.com/v1/'
        assert client.max_retries == 0 and client._client.follow_redirects is False
        assert client._client._transport._pool._retries == 0

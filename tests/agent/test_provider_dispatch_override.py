"""Reject unknown per-client dispatch overrides rather than bypass the guard."""
import httpx
import pytest
from agent import auxiliary_client as aux
from agent.bounded_completion import CompletionHeld
from tests.agent.test_provider_security_corrected import route_fixture,response_for,run


@pytest.mark.parametrize('provider',['openai','anthropic','gemini','openai-api','openai-codex'])
@pytest.mark.parametrize('override',['route','send'])
def test_unknown_instance_dispatch_cannot_bypass_transport_fence(monkeypatch,provider,override):
    requests=[]
    def respond(req):requests.append(req);return response_for(provider)
    config=route_fixture(monkeypatch,provider,respond)
    def factory(*args,**kw):
        if not kw.get('async_mode'):return {'http_client':httpx.Client(transport=httpx.MockTransport(respond))}
        raw=httpx.MockTransport(respond)
        client=httpx.AsyncClient(transport=raw)
        if override=='route':client._transport_for_url=lambda url:raw
        else:
            original=client._send_single_request
            async def send(request):return await original(request)
            client._send_single_request=send
        return {'http_client':client}
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',factory)
    with pytest.raises(CompletionHeld,match='native_dispatch_boundary_unavailable'):
        run(config,timeout=2)
    assert not requests

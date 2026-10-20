"""Real HTTPX dispatch after SDK/build/hooks, base and mounted synthetic wires."""
import asyncio
import time
import httpx
import pytest
from agent import auxiliary_client as aux
from tests.agent.test_provider_security_corrected import route_fixture,response_for,run


@pytest.mark.parametrize('provider',['openai','anthropic','gemini','openai-api','openai-codex'])
@pytest.mark.parametrize('routing',['default','mounted'])
@pytest.mark.parametrize('boundary',['positive','build-deadline','hook-deadline','build-cancel'])
def test_transport_dispatch_fence_preserves_route_and_rejects_late_or_cancelled(monkeypatch,provider,routing,boundary):
    requests=[]
    def respond(request):
        requests.append((request,time.monotonic()))
        return response_for(provider)
    config=route_fixture(monkeypatch,provider,respond)
    hooks=[]
    def factory(*args,**kw):
        if not kw.get('async_mode'):return {'http_client':httpx.Client(transport=httpx.MockTransport(respond))}
        if routing=='mounted':
            def forbidden(request):raise AssertionError('mounted route fell back to default transport')
            return {'http_client':httpx.AsyncClient(transport=httpx.MockTransport(forbidden),
                mounts={'all://':httpx.MockTransport(respond)},event_hooks={'request':list(hooks)})}
        return {'http_client':httpx.AsyncClient(transport=httpx.MockTransport(respond),event_hooks={'request':list(hooks)})}
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',factory)
    assert run(config,timeout=2).text=='synthetic'
    requests.clear()
    original=httpx.AsyncClient.build_request
    def build(self,*args,**kwargs):
        if boundary=='build-deadline':time.sleep(.06)
        if boundary=='positive':time.sleep(.005)
        request=original(self,*args,**kwargs)
        if boundary=='build-cancel':asyncio.current_task().cancel()
        return request
    monkeypatch.setattr(httpx.AsyncClient,'build_request',build)
    if boundary=='hook-deadline':
        async def delayed(request):time.sleep(.06)
        hooks.append(delayed)
    started=time.monotonic()
    if boundary=='positive':
        result=run(config,timeout=.5)
        assert result.text=='synthetic' and result.limits.max_physical_requests==1
        assert len(requests)==1 and requests[0][1]-started<.5
        request=requests[0][0]
        expected={'openai':'api.openai.com','anthropic':'api.anthropic.com',
            'gemini':'generativelanguage.googleapis.com','openai-api':'api.openai.com',
            'openai-codex':'chatgpt.com'}[provider]
        assert request.url.host==expected
        assert any(k in request.headers for k in ['authorization','x-api-key','x-goog-api-key'])
    else:
        with pytest.raises(asyncio.CancelledError if boundary=='build-cancel' else TimeoutError):
            run(config,timeout=.03 if boundary!='build-cancel' else .5)
        assert not requests,'late/cancelled request reached actual selected transport'

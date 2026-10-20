"""Synthetic HTTP observations, not live Codex service or bounded presence proof.

Preserves verifier positive fixtures: actual auxiliary resolver/SDK serialization
and actual main Codex current-input/cache-prefix behavior. The general threaded
auxiliary is deliberately NOT admitted for bounded presence.
"""
import asyncio
import base64
import json

import httpx
import openai
import pytest
from agent import auxiliary_client as aux

MODEL = 'gpt-6-astra-900k'



def synthetic_sse(text='synthetic provider result'):
    item = {'type':'message', 'id':'msg_synthetic', 'role':'assistant',
            'status':'completed', 'content':[{'type':'output_text','text':text,'annotations':[]}]}
    response = {'id':'resp_synthetic','object':'response','created_at':0,
                'status':'completed','model':'gpt-6-astra','output':[item],
                'usage':{'input_tokens':12,'output_tokens':600,'total_tokens':612}}
    events = [{'type':'response.output_item.done','output_index':0,'item':item},
              {'type':'response.completed','response':response}]
    return httpx.Response(200, headers={'Content-Type':'text/event-stream'},
                          content=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n')


def configure_codex(monkeypatch, handler):
    payload = base64.urlsafe_b64encode(json.dumps({
        'https://api.openai.com/auth':{'chatgpt_account_id':'synthetic-account'}}).encode()).rstrip(b'=')
    monkeypatch.setattr(aux,'_select_pool_entry',lambda _provider:(False,None))
    monkeypatch.setattr(aux,'_read_codex_access_token',lambda:'e30.'+payload.decode()+'.synthetic-signature')
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',lambda _url, **kwargs:{
        'http_client':httpx.Client(transport=httpx.MockTransport(handler))})
    # Keep real resolver, token-to-account headers, SDK construction, Responses
    # serialization, stream decoder, async bridge, and factory retry policy.
    return aux.resolve_provider_client('openai-codex',model=MODEL,async_mode=True)


def test_real_codex_resolver_native_sdk_and_responses_serialization(monkeypatch):
    calls=[]
    def respond(request):
        calls.append(request)
        return synthetic_sse()
    client,model=configure_codex(monkeypatch,respond)
    assert type(client) is aux.AsyncCodexAuxiliaryClient
    try:
        response=asyncio.run(client.chat.completions.create(model=model,messages=[
            {'role':'system','content':'Synthetic capsule, not instructions from evidence.'},
            {'role':'user','content':'synthetic evidence'}],max_tokens=512,timeout=5))
        assert response.choices[0].message.content=='synthetic provider result'
        assert len(calls)==1
        wire=json.loads(calls[0].content)
        assert wire['model']=='gpt-6-astra' and wire['store'] is False and wire['stream'] is True
        assert wire['instructions'].startswith('Synthetic capsule')
        assert wire['input'][0]['role']=='user'
        assert calls[0].headers['chatgpt-account-id']=='synthetic-account'
        assert client._real_client.max_retries==0
        # Existing Codex explicitly omits server token cap; observe, do not hide.
        assert 'max_output_tokens' not in wire and response.usage.completion_tokens==600
    finally:
        client._real_client.close()


def test_real_codex_429_factory_has_one_physical_attempt(monkeypatch):
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(429,json={'error':{'message':'synthetic rate limit'}})
    client,model=configure_codex(monkeypatch,respond)
    assert client is not None
    try:
        with pytest.raises(openai.RateLimitError):
            asyncio.run(client.chat.completions.create(model=model,messages=[{'role':'user','content':'test'}],timeout=1))
        assert len(calls)==1
    finally:
        client._real_client.close()


def test_main_codex_provider_can_preserve_presence_current_context_and_prefix(monkeypatch,tmp_path):
    from tests.gateway.test_presence_integration import setup_host
    from run_agent import AIAgent
    native_factory=AIAgent._create_openai_client
    h,aux_requests,bot=setup_host(monkeypatch,tmp_path)
    main_requests=[]
    def respond(request):
        main_requests.append(json.loads(request.content))
        return synthetic_sse('ack')
    monkeypatch.setattr(AIAgent,'_create_openai_client',native_factory)
    monkeypatch.setattr(AIAgent,'_build_keepalive_http_client',staticmethod(
        lambda *a,**kw:httpx.Client(transport=httpx.MockTransport(respond))))
    runtime={'api_key':'synthetic-codex-oauth-token','base_url':'https://chatgpt.com/backend-api/codex',
             'provider':'openai-codex','api_mode':'codex_responses'}
    h.runner._resolve_session_agent_runtime=lambda **kw:(MODEL,dict(runtime))
    try:
        text='The previous attempt failed and I am frustrated. Please be brief.'
        h.inbound_human(text,message_id=100)
        assert len(main_requests)==1 and len(aux_requests)==1
        first=main_requests[0]
        assert first['model']=='gpt-6-astra' and first['store'] is False
        serialized=json.dumps(first['input'],sort_keys=True)
        assert 'Tentative interaction posture: careful_brief' in serialized
        assert h.user_rows()[-1]['content']==text
        h.inbound_human('Presence: ordinary',message_id=101)
        assert len(main_requests)==2
        second=main_requests[1]
        assert first['instructions']==second['instructions']
        assert second['input'][:len(first['input'])]==first['input']
        assert 'Tentative interaction posture' not in json.dumps(second['input'][-1])
        assert bot.send_message.await_count==0
    finally:
        h.close()

"""Real native serializers over synthetic async HTTP; no credentials/network."""
import asyncio
import json
import httpx
import pytest
from agent import auxiliary_client as aux
from agent.bounded_completion import complete


@pytest.mark.parametrize('provider', ['anthropic','gemini','openai-codex'])
def test_native_request_mode_preserves_roles_auth_and_unknown_usage(monkeypatch,provider):
    calls=[]
    async def respond(request):
        calls.append(request)
        if provider=='anthropic':
            body={'content':[{'type':'text','text':'synthetic'}]}
        elif provider=='gemini':
            body={'candidates':[{'content':{'role':'model','parts':[{'text':'synthetic'}]}}]}
        else:
            event={'type':'response.completed','response':{'output':[{'type':'message','content':[{'type':'output_text','text':'synthetic'}]}]}}
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content='data: '+json.dumps(event)+'\n\n')
        return httpx.Response(200,json=body)
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',lambda *a,**kw:{'http_client':
        httpx.AsyncClient(transport=httpx.MockTransport(respond))} if kw.get('async_mode') else {})
    if provider=='anthropic':
        import anthropic
        native=anthropic.Anthropic(api_key='synthetic-key')
        wrapper=aux.AnthropicAuxiliaryClient(native,'configured-model','synthetic-key','https://api.anthropic.com',False)
    elif provider=='gemini':
        from agent.gemini_native_adapter import GeminiNativeClient
        wrapper=GeminiNativeClient(api_key='synthetic-key')
    else:
        import openai
        wrapper=openai.OpenAI(api_key='synthetic-oauth',base_url='https://chatgpt.com/backend-api/codex',
            default_headers={'chatgpt-account-id':'synthetic-account'})
    monkeypatch.setattr(aux,'resolve_provider_client',lambda *a,**kw:(wrapper,'configured-model'))
    messages=[{'role':'system','content':'stable system'},{'role':'user','content':'real input'}]
    result=asyncio.run(complete(config={'presence':{'budget_mode':'requests'},'auxiliary':{'presence':{
        'provider':provider,'model':'configured-model'}}},messages=messages,max_output_tokens=64,timeout=1))
    assert result.text=='synthetic' and result.output_tokens is None and result.usage=='unknown'
    assert messages==[{'role':'system','content':'stable system'},{'role':'user','content':'real input'}]
    assert len(calls)==1
    wire=json.loads(calls[0].content)
    if provider=='anthropic':
        assert wire['messages'][0]['role']=='user' and wire['max_tokens']==64
        assert calls[0].headers['x-api-key']=='synthetic-key'
    elif provider=='gemini':
        assert wire['contents'][0]['role']=='user' and wire['generationConfig']['maxOutputTokens']==64
        assert calls[0].headers['x-goog-api-key']=='synthetic-key'
    else:
        assert wire['instructions']=='stable system' and wire['input'][0]['role']=='user'
        assert 'max_output_tokens' not in wire and wire['store'] is False
        assert calls[0].headers['chatgpt-account-id']=='synthetic-account'

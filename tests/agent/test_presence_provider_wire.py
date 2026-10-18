"""Real resolver/auth owner/native serializers with HTTP-only synthetic boundary."""
import asyncio
import base64
import json
import httpx
import pytest
import openai
from agent import auxiliary_client as aux
from agent.bounded_completion import complete

PROVIDERS=['openai','anthropic','gemini','openai-codex','openai-api']


def route_fixture(monkeypatch,provider,respond):
    monkeypatch.setattr(aux,'_select_pool_entry',lambda *a:(False,None))
    account=base64.urlsafe_b64encode(json.dumps({'https://api.openai.com/auth':{
        'chatgpt_account_id':'synthetic-account'}}).encode()).decode().rstrip('=')
    monkeypatch.setattr(aux,'_read_codex_access_token',lambda:'e30.'+account+'.synthetic-signature')
    monkeypatch.setattr('agent.anthropic_adapter.resolve_anthropic_token',lambda:'synthetic-key')
    monkeypatch.setattr(aux,'_scoped_key_env',lambda key:'synthetic-key' if key=='OPENAI_API_KEY' else None)
    # Only the secret-read boundary is synthetic; credential precedence, prefix
    # validation, endpoint and provider identity use the genuine auth owner.
    secrets={'GOOGLE_API_KEY':'AIza'+'synthetic-presence-fixture-not-a-credential',
             'GEMINI_API_KEY':'AIza'+'synthetic-presence-fixture-not-a-credential',
             'OPENAI_API_KEY':'sk-'+'synthetic-presence-fixture-not-a-credential'}
    monkeypatch.setattr('clover_cli.config.get_env_value_prefer_dotenv',lambda key:secrets.get(key))
    def http_factory(*a,**kw):
        return {'http_client':(httpx.AsyncClient if kw.get('async_mode') else httpx.Client)(
            transport=httpx.MockTransport(respond),follow_redirects=True)}
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',http_factory)
    return {'presence':{'budget_mode':'requests'},'auxiliary':{'presence':{
        'provider':provider,'model':'claude-haiku-4-5' if provider=='anthropic' else
        'gemini-2.5-flash' if provider=='gemini' else 'gpt-4o-mini' if provider in {'openai','openai-api'} else 'gpt-6-astra-900k'}}}


def response_for(provider):
    if provider=='openai':
        return httpx.Response(200,json={'choices':[{'message':{'content':'synthetic'}}],
            'usage':{'completion_tokens':83,'completion_tokens_details':{'reasoning_tokens':70}}})
    if provider=='anthropic':
        return httpx.Response(200,json={'content':[{'type':'text','text':'synthetic'}], 'usage':{'output_tokens':83}})
    if provider=='gemini':
        return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':'synthetic'}]}}],
            'usageMetadata':{'candidatesTokenCount':13,'thoughtsTokenCount':70}})
    event={'type':'response.completed','response':{'output':[{'type':'message','content':[{'type':'output_text','text':'synthetic'}]}],
        'usage':{'output_tokens':83,'output_tokens_details':{'reasoning_tokens':70}}}}
    return httpx.Response(200,headers={'Content-Type':'text/event-stream'},content='data: '+json.dumps(event)+'\n\n')


@pytest.mark.parametrize('provider',PROVIDERS)
def test_real_configured_resolver_serializes_once_with_no_model_or_account_fallback(monkeypatch,provider):
    requests=[]
    def respond(request):
        requests.append(request); return response_for(provider)
    config=route_fixture(monkeypatch,provider,respond)
    messages=[{'role':'system','content':'stable system'},{'role':'user','content':'current user'}]
    result=asyncio.run(complete(config=config,messages=messages,max_output_tokens=64,timeout=2))
    assert result.text=='synthetic' and result.output_tokens==83
    assert len(requests)==1 and messages[0]['content']=='stable system'
    # Request mode does not lie about a provider reporting more than the envelope.
    if provider=='openai-codex':
        assert requests[0].headers['chatgpt-account-id']=='synthetic-account'
        wire=json.loads(requests[0].content)
        assert wire['model']=='gpt-6-astra' and 'max_output_tokens' not in wire
    if provider in {'gemini','openai','openai-codex'}:
        assert result.reasoning_tokens==70


@pytest.mark.parametrize('provider',PROVIDERS)
@pytest.mark.parametrize('status',[429,301,302,303,307,308])
def test_real_provider_http_failures_never_retry_or_follow_redirect(monkeypatch,provider,status):
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(status,headers={'location':'https://never.invalid/retry'},json={'error':{'message':'synthetic'}})
    config=route_fixture(monkeypatch,provider,respond)
    with pytest.raises((ValueError,httpx.HTTPStatusError,openai.APIStatusError)):
        asyncio.run(complete(config=config,messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))
    assert len(requests)==1


@pytest.mark.parametrize('provider',PROVIDERS)
def test_real_provider_late_chunk_cancel_closes_local_transport_and_discards(monkeypatch,provider):
    closed=[];started=[]
    class Late(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.append(True)
            yield b'data: ' if provider=='openai-codex' else b'{'
            await asyncio.Event().wait()
            yield b'late result'
        async def aclose(self):closed.append(True)
    def respond(request):return httpx.Response(200,stream=Late())
    config=route_fixture(monkeypatch,provider,respond)
    with pytest.raises(TimeoutError):
        asyncio.run(complete(config=config,messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=.2))
    assert started==[True] and closed==[True]


@pytest.mark.parametrize('provider',PROVIDERS)
def test_real_provider_response_byte_envelope_closes_without_publishing(monkeypatch,provider):
    closed=[]
    class Large(httpx.AsyncByteStream):
        async def __aiter__(self):yield b'x'*16385
        async def aclose(self):closed.append(True)
    config=route_fixture(monkeypatch,provider,lambda request:httpx.Response(200,stream=Large()))
    with pytest.raises(ValueError,match='byte_envelope'):
        asyncio.run(complete(config=config,messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))
    assert closed==[True]


@pytest.mark.parametrize('provider',['openai','anthropic','openai-api'])
def test_strict_contract_violation_is_not_a_success_or_zero_usage(monkeypatch,provider):
    config=route_fixture(monkeypatch,provider,lambda request:response_for(provider))
    config['presence']['budget_mode']='strict_tokens'
    with pytest.raises(ValueError,match='provider_exceeded_output_contract'):
        asyncio.run(complete(config=config,messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))


def test_openai_error_body_is_bounded_before_sdk_eager_error_decode(monkeypatch):
    chunks=[];closed=[]
    class ErrorBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for n in range(300):
                chunks.append(n);yield b'x'*100
        async def aclose(self):closed.append(True)
    config=route_fixture(monkeypatch,'openai',lambda request:httpx.Response(429,stream=ErrorBody()))
    with pytest.raises(ValueError,match='byte_envelope'):
        asyncio.run(complete(config=config,messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))
    assert len(chunks)<=164 and closed==[True]


@pytest.mark.parametrize('provider',['anthropic','gemini'])
def test_explicit_model_never_probes_a_provider_auxiliary_default(monkeypatch,provider):
    config=route_fixture(monkeypatch,provider,lambda request:response_for(provider))
    monkeypatch.setattr(aux,'_get_aux_model_for_provider',lambda *a,**kw:pytest.fail('explicit model queried a catalog default'))
    result=asyncio.run(complete(config=config,messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))
    assert result.route.model==config['auxiliary']['presence']['model']


@pytest.mark.parametrize('field',['model','api_key'])
def test_presence_entry_rechecks_configured_route_after_http_without_re_resolving_accounts(monkeypatch,field):
    config={};requests=[]
    def respond(request):
        requests.append(request)
        config['auxiliary']['presence'][field]='synthetic-changed-setting'
        return response_for('openai')
    config.update(route_fixture(monkeypatch,'openai',respond))
    monkeypatch.setattr('clover_cli.config.load_config',lambda:config)
    with pytest.raises(ValueError,match='configured_route_changed'):
        asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))
    assert len(requests)==1


def test_official_api_responses_strict_wire_is_not_a_codex_account_route(monkeypatch):
    requests=[]
    def respond(request):requests.append(request);return response_for('openai-api')
    config=route_fixture(monkeypatch,'openai-api',respond);config['presence']['budget_mode']='strict_tokens'
    result=asyncio.run(complete(config=config,messages=[{'role':'system','content':'stable prefix'},
        {'role':'user','content':'real current input'}],max_output_tokens=128,timeout=1))
    assert result.text=='synthetic' and result.limits.server_token_guarantee is True
    assert str(requests[0].url)=='https://api.openai.com/v1/responses'
    wire=json.loads(requests[0].content)
    assert wire['max_output_tokens']==128 and wire['store'] is False and wire['stream'] is True
    assert wire['instructions']=='stable prefix' and wire['input'][0]['role']=='user'
    assert 'chatgpt-account-id' not in requests[0].headers and len(requests)==1

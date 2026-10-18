"""Offline capability and real transport tests; never a live provider claim."""
import asyncio
import json
import httpx
import openai
import pytest
from agent import auxiliary_client as aux


def test_auto_means_configured_route_not_provider_detection(monkeypatch):
    from agent.bounded_completion import select_route
    config = {'model': {'provider': 'anthropic', 'default': 'configured-model'},
              'auxiliary': {'presence': {'provider': 'auto'}}}
    route = select_route(config)
    assert (route.provider, route.model, route.mode) == ('anthropic', 'configured-model', 'strict_tokens')
    config['auxiliary']['presence'] = {'provider': 'gemini', 'model': 'explicit-model'}
    assert select_route(config).provider == 'gemini'


def test_unverified_cap_holds_without_credentials(monkeypatch):
    monkeypatch.setattr('clover_cli.config.load_config', lambda: {
        'model': {'provider': 'openai-codex', 'default': 'selected-model'}})
    monkeypatch.setattr(aux, 'resolve_provider_client', lambda *a, **k: pytest.fail('credential resolution'))
    with pytest.raises(ValueError, match='output.*cap'):
        asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'test'}], max_output_tokens=64, timeout=1))


def test_request_mode_has_usage_unknown_not_zero_and_bounded_wire(monkeypatch):
    from agent.bounded_completion import complete
    calls=[]
    async def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'synthetic'}}]})
    client=openai.AsyncOpenAI(api_key='synthetic',base_url='http://localhost:1234/v1',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(aux,'resolve_provider_client',lambda *a,**k:(client,'local-model'))
    config={'presence':{'budget_mode':'requests'},'auxiliary':{'presence':{
        'provider':'custom','model':'local-model','base_url':'http://localhost:1234/v1','api_key':'synthetic'}}}
    result=asyncio.run(complete(config=config,messages=[{'role':'user','content':'test'}], max_output_tokens=64,timeout=1))
    assert result.text=='synthetic' and result.output_tokens is None and result.usage=='unknown'
    assert result.capabilities.server_output_cap=='unverified'
    assert result.mode=='requests' and result.capabilities.cancellation=='close_connection'
    assert result.limits.max_physical_requests == 1 and result.limits.requested_output_tokens == 64
    assert result.limits.deadline_seconds == 1 and result.limits.max_response_bytes == 16384
    assert result.limits.server_token_guarantee is False
    assert len(calls)==1 and client.is_closed()
    assert json.loads(calls[0].content).get('max_tokens')==64


def test_strict_cap_cannot_be_inherited_from_an_unverified_resolved_host(monkeypatch):
    from agent.bounded_completion import complete
    calls=[]
    async def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'unverified'}}]})
    client=openai.AsyncOpenAI(api_key='synthetic',base_url='https://unverified.invalid/v1',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(aux,'resolve_provider_client',lambda *a,**k:(client,'configured'))
    with pytest.raises(ValueError,match='resolved_endpoint_cap_unverified'):
        asyncio.run(complete(config={'auxiliary':{'presence':{'provider':'openai','model':'configured'}}},
            messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))
    assert not calls and client.is_closed()


def test_disabled_fallback_resolver_does_not_detect_any_other_provider(monkeypatch):
    monkeypatch.setattr(aux,'_try_custom_endpoint',lambda *a,**kw:pytest.fail('custom detection'))
    monkeypatch.setattr(aux,'_resolve_api_key_provider',lambda *a,**kw:pytest.fail('provider fallback'))
    assert aux.resolve_provider_client('custom',model='configured',allow_fallback=False)==(None,None)
    assert aux.resolve_provider_client('auto',model='configured',allow_fallback=False)==(None,None)


@pytest.mark.parametrize('provider', ['openai-codex','anthropic','openrouter'])
def test_exhausted_pool_cannot_select_a_different_credential(monkeypatch,provider):
    monkeypatch.setattr(aux,'_select_pool_entry',lambda *a,**kw:(True,None))
    monkeypatch.setattr(aux,'_read_codex_access_token',lambda:pytest.fail('account fallback'))
    monkeypatch.setattr(aux,'_scoped_key_env',lambda *a:pytest.fail('account fallback'))
    monkeypatch.setattr('agent.anthropic_adapter.resolve_anthropic_token',lambda:pytest.fail('account fallback'))
    assert aux.resolve_provider_client(provider,model='configured',allow_fallback=False)==(None,None)


@pytest.mark.parametrize('provider',['custom','lmstudio'])
def test_real_local_resolver_and_physical_loopback_request(monkeypatch,provider):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from agent.bounded_completion import complete
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            payload=json.dumps({'choices':[{'message':{'content':'local synthetic'}}],
                'usage':{'completion_tokens':9}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def log_message(self,*a):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(aux,'_scoped_key_env',lambda *a:None)
    try:
        base='http://127.0.0.1:'+str(server.server_port)+'/v1'
        result=asyncio.run(complete(config={'presence':{'budget_mode':'requests'},'model':{
            'provider':provider,'default':'local-model','base_url':base}},messages=[{'role':'system','content':'stable'},
            {'role':'user','content':'local input'}],max_output_tokens=64,timeout=2))
        assert result.text=='local synthetic' and len(calls)==1 and result.route.provider==provider
        assert calls[0][0]=='/v1/chat/completions'
        assert calls[0][1]['model']=='local-model' and calls[0][1].get('max_tokens')==64
    finally:
        server.shutdown();server.server_close();thread.join(3)
        assert not thread.is_alive()


@pytest.mark.parametrize('roles',[['user','user'],['assistant','assistant'],['user','system']])
def test_invalid_role_sequence_holds_before_credentials(monkeypatch,roles):
    from agent.bounded_completion import complete
    monkeypatch.setattr(aux,'resolve_provider_client',lambda *a,**kw:pytest.fail('invalid roles resolved credentials'))
    with pytest.raises(ValueError,match='role_sequence'):
        asyncio.run(complete(config={'auxiliary':{'presence':{'provider':'openai','model':'configured'}}},
            messages=[{'role':r,'content':'text'} for r in roles],max_output_tokens=64,timeout=1))


@pytest.mark.parametrize('route',[
    {'provider':'openai','model':'configured','api_mode':'codex_app_server'},
    {'provider':'anthropic','model':'configured','base_url':'https://override.invalid'},
])
def test_unsupported_transport_selection_holds_without_credential_side_effects(monkeypatch,route):
    from agent.bounded_completion import complete
    monkeypatch.setattr(aux,'resolve_provider_client',lambda *a,**kw:pytest.fail('unsupported transport resolved credentials'))
    with pytest.raises(ValueError,match='transport_selection_unsupported'):
        asyncio.run(complete(config={'presence':{'budget_mode':'requests'},'auxiliary':{'presence':route}},
            messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))


def test_configured_app_server_is_not_silently_replaced_with_http(monkeypatch):
    from agent.bounded_completion import complete
    monkeypatch.setattr(aux,'resolve_provider_client',lambda *a,**kw:pytest.fail('configured runtime replaced'))
    with pytest.raises(ValueError,match='transport_selection_unsupported'):
        asyncio.run(complete(config={'presence':{'budget_mode':'requests'},'model':{
            'provider':'openai-codex','default':'configured','openai_runtime':'codex_app_server'}},
            messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))


def test_api_key_owner_does_not_iterate_other_accounts_when_fallback_disabled(monkeypatch):
    from types import SimpleNamespace
    from clover_cli import auth
    monkeypatch.setattr('clover_cli.config.get_env_value_prefer_dotenv',lambda *a:None)
    pool=SimpleNamespace(has_credentials=lambda:True,peek=lambda:SimpleNamespace(access_token=''),
        entries=lambda:pytest.fail('secondary account selected'))
    monkeypatch.setattr('agent.credential_pool.load_pool',lambda *a:pool)
    result=auth.resolve_api_key_provider_credentials('gemini',allow_fallback=False)
    assert result['api_key']==''


def test_explicit_endpoint_cannot_be_ignored_by_a_provider_resolver(monkeypatch):
    from agent.bounded_completion import complete
    calls=[]
    async def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'wrong route'}}]})
    client=openai.AsyncOpenAI(api_key='synthetic-key',base_url='https://ignored-route.invalid/v1',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(aux,'resolve_provider_client',lambda *a,**kw:(client,'configured'))
    with pytest.raises(ValueError,match='configured_endpoint_changed'):
        asyncio.run(complete(config={'presence':{'budget_mode':'requests'},'auxiliary':{'presence':{
            'provider':'openrouter','model':'configured','base_url':'https://explicit.invalid/v1'}}},
            messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))
    assert not calls and client.is_closed()


def test_auto_model_is_not_repaired_with_a_catalog_default(monkeypatch):
    from agent.bounded_completion import complete
    monkeypatch.setattr(aux,'resolve_provider_client',lambda *a,**kw:pytest.fail('auto model reached credential resolver'))
    with pytest.raises(ValueError,match='configured_provider_and_model_required'):
        asyncio.run(complete(config={'model':{'provider':'openai','default':'auto'}},
            messages=[{'role':'user','content':'test'}],max_output_tokens=64,timeout=1))


@pytest.mark.parametrize('provider,base,field',[('openai-api',None,'max_output_tokens'),
    ('custom','https://api.openai.com/v1','max_completion_tokens')])
def test_strict_capability_follows_official_wire_endpoint_not_provider_label(provider,base,field):
    from agent.bounded_completion import Route,admitted
    cap=admitted(Route(provider,'configured-model',base,'strict_tokens'))
    assert cap.server_output_cap=='verified' and cap.output_limit_parameter==field


@pytest.mark.parametrize('provider',['anthropic','openrouter'])
def test_explicit_credential_precedes_pool_selection_without_touching_other_accounts(monkeypatch,provider):
    monkeypatch.setattr(aux,'_select_pool_entry',lambda *a,**kw:pytest.fail('explicit key selected another account'))
    client,model=aux.resolve_provider_client(provider,model='configured-model',explicit_api_key='synthetic-explicit-key',
        allow_fallback=False)
    assert model=='configured-model' and client.api_key=='synthetic-explicit-key'
    client.close()


def test_explicit_api_key_owner_does_not_read_ambient_credentials(monkeypatch):
    from clover_cli import auth
    monkeypatch.setattr(auth,'_resolve_api_key_provider_secret',lambda *a,**kw:pytest.fail('explicit key read ambient account'))
    creds=auth.resolve_api_key_provider_credentials('gemini',allow_fallback=False,explicit_api_key='synthetic-explicit-key')
    assert creds['api_key']=='synthetic-explicit-key' and creds['source']=='explicit'


@pytest.mark.parametrize('provider',['zai','kimi-coding','kimi-coding-cn','copilot'])
def test_unbounded_endpoint_or_token_discovery_is_held_before_credential_reads(monkeypatch,provider):
    from clover_cli import auth
    monkeypatch.setattr(auth,'_resolve_api_key_provider_secret',lambda *a,**kw:pytest.fail('unverified owner read credentials'))
    monkeypatch.setattr(auth.os,'getenv',lambda *a,**kw:'')
    with pytest.raises(auth.AuthError) as caught:
        auth.resolve_api_key_provider_credentials(provider,allow_fallback=False)
    assert caught.value.code in {'endpoint_selection_unverified','credential_owner_unverified'}


def test_explicit_owned_endpoint_never_runs_regional_detection(monkeypatch):
    from clover_cli import auth
    monkeypatch.setattr(auth,'_resolve_zai_base_url',lambda *a,**kw:pytest.fail('regional endpoint probe'))
    creds=auth.resolve_api_key_provider_credentials('zai',allow_fallback=False,
        explicit_api_key='synthetic-explicit-key',explicit_base_url='https://chosen.invalid/v1')
    assert creds['base_url']=='https://chosen.invalid/v1' and creds['source']=='explicit'

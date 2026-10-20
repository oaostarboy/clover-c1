"""Independent extra negative probes: synthetic HTTP/keys only."""
import asyncio
import json
import time
import httpx
import pytest
from agent import auxiliary_client as aux
from agent.bounded_completion import CompletionHeld
from tests.agent.test_provider_security_corrected import route_fixture, response_for, run

@pytest.mark.parametrize('name', ['beans', 'openai', 'gemini', 'anthropic'])
@pytest.mark.parametrize('base', ['http://127.0.0.1:8080/v1', 'https://unrelated-owner.invalid/tenant-b/v1'])
def test_configured_keyless_alias_never_uses_builtin_secret(monkeypatch, name, base):
    requests=[]
    config=route_fixture(monkeypatch,'openai',lambda req: requests.append(req) or response_for('openai'))
    config['model']={'provider':'openai-api','default':'main','base_url':'https://api.openai.com/v1'}
    config['providers']={name:{'base_url':base}}
    config['auxiliary']['presence']={'provider':name,'model':'literal-local-model'}
    secrets={'OPENAI_API_KEY':'sk-synthetic-unrelated-openai',
             'GOOGLE_API_KEY':'AIzasynthetic-unrelated-google',
             'GEMINI_API_KEY':'AIzasynthetic-unrelated-google',
             'ANTHROPIC_API_KEY':'sk-ant-synthetic-unrelated-anthropic'}
    monkeypatch.setattr(aux,'_scoped_key_env',lambda env: secrets.get(env))
    monkeypatch.setattr('clover_cli.config.get_env_value_prefer_dotenv',lambda env: secrets.get(env))
    assert run(config).text=='synthetic'
    assert len(requests)==1
    auth=requests[0].headers.get('authorization')
    print('SYNTHETIC_WIRE',name,str(requests[0].url),auth)
    assert auth=='Bearer no-key-required'

@pytest.mark.parametrize('phase', ['initial-config','publication-config','publication-authority'])
def test_public_entry_includes_all_sync_work_in_deadline(monkeypatch,phase):
    from agent.bounded_auth import Authority
    requests=[]
    config=route_fixture(monkeypatch,'openai',lambda req: requests.append(req) or response_for('openai'))
    config['auxiliary']['presence']['api_key']='synthetic-owned'
    reads=[]
    def load():
        reads.append(True)
        if (phase=='initial-config' and len(reads)==1) or (phase=='publication-config' and len(reads)==2):
            time.sleep(.25)
        return config
    monkeypatch.setattr('clover_cli.config.load_config',load)
    original=Authority.validate
    validations=[]
    def validate(self):
        validations.append(True)
        if phase=='publication-authority' and len(validations)==3:
            time.sleep(.25)
        return original(self)
    monkeypatch.setattr(Authority,'validate',validate)
    start=time.monotonic()
    try:
        text=asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'synthetic'}],max_output_tokens=64,timeout=.15))
    except TimeoutError:
        print('EXPECTED_HOLD',phase,'elapsed',time.monotonic()-start,'requests',len(requests))
        return
    elapsed=time.monotonic()-start
    print('PUBLIC_RETURN',phase,'elapsed',elapsed,'text',text,'config_reads',len(reads),'validations',len(validations))
    pytest.fail('public entry returned successful text after its deadline')

@pytest.mark.parametrize('change', ['unchanged','token-cleared','endpoint','expiry'])
def test_selected_persisted_owner_disagrees_before_dispatch(monkeypatch,tmp_path,change):
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    from clover_constants import get_clover_home
    monkeypatch.setenv('CLOVER_HOME',str(tmp_path/'profile'))
    config,pool,requests=genuine_pool_route(monkeypatch,[entry('a')])
    assert pool.select().id=='a'  # genuine main selection before disk mutation
    selected=pool.current()
    assert selected is not None
    disk=[selected.to_dict()]
    if change=='token-cleared': disk[0]['access_token']=''
    elif change=='endpoint': disk[0]['base_url']='https://replacement-owner.invalid/v1'
    elif change=='expiry': disk[0]['expires_at_ms']=1
    home=get_clover_home()
    home.mkdir(parents=True,exist_ok=True)
    (home/'auth.json').write_text(json.dumps({'credential_pool':{'openai-codex':disk}}))
    try:
        result=run(config)
    except CompletionHeld:
        assert change!='unchanged'
        assert not requests
        return
    print('PERSISTED_OWNER_DISAGREEMENT',change,'text',result.text,'requests',requests)
    assert result.text=='synthetic' and len(requests)==1
    if change!='unchanged':
        pytest.fail('inference used cached selected owner after persisted authority changed')


def test_actual_disk_selected_removal_during_http_holds(monkeypatch,tmp_path):
    from tests.agent.test_provider_security_corrected import genuine_pool_route,entry
    from clover_constants import get_clover_home
    monkeypatch.setenv('CLOVER_HOME',str(tmp_path/'profile'))
    config,pool,requests=genuine_pool_route(monkeypatch,[entry('a')])
    assert pool.select().id=='a'  # genuine main selection before disk mutation
    selected=pool.current()
    assert selected is not None
    home=get_clover_home()
    home.mkdir(parents=True,exist_ok=True)
    file=home/'auth.json'
    file.write_text(json.dumps({'credential_pool':{'openai-codex':[selected.to_dict()]}}))
    def respond(req):
        requests.append(('inference',str(req.url),req.headers.get('chatgpt-account-id')))
        file.write_text(json.dumps({'credential_pool':{'openai-codex':[]}}))
        return response_for('openai-codex')
    original=aux._openai_http_client_kwargs
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',lambda *a,**kw:
        {'http_client':httpx.AsyncClient(transport=httpx.MockTransport(respond))} if kw.get('async_mode') else original(*a,**kw))
    with pytest.raises(CompletionHeld,match='authority_changed'):
        run(config)
    assert len(requests)==1

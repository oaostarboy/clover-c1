"""Deadline, persisted-owner and alias boundaries, using synthetic HTTP only."""
import asyncio
import json
import time

import httpx
import pytest

from agent import auxiliary_client as aux
from agent.bounded_completion import CompletionHeld
from tests.agent.test_provider_security_corrected import route_fixture, response_for, run


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'gemini', 'openai-api'])
def test_http_timeout_uses_remaining_public_budget(monkeypatch, provider):
    requests = []
    config = route_fixture(monkeypatch, provider, lambda req: requests.append(req) or response_for(provider))
    def load():
        time.sleep(.08)
        return config
    monkeypatch.setattr('clover_cli.config.load_config', load)
    text = asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'synthetic'}],
                                               max_output_tokens=64, timeout=2))
    assert text == 'synthetic' and len(requests) == 1
    assert 0 < requests[0].extensions['timeout']['read'] < 1.95


def test_native_serializer_cannot_dispatch_after_absolute_deadline(monkeypatch):
    from agent import gemini_native_adapter
    requests = []
    config = route_fixture(monkeypatch, 'gemini', lambda req: requests.append(req) or response_for('gemini'))
    original = gemini_native_adapter.build_gemini_request
    def slow(**kwargs):
        time.sleep(.04)
        return original(**kwargs)
    monkeypatch.setattr(gemini_native_adapter, 'build_gemini_request', slow)
    with pytest.raises(TimeoutError):
        run(config, timeout=.02)
    assert not requests


@pytest.mark.parametrize('change', ['unchanged', 'token-missing', 'token-empty', 'token-other',
    'endpoint', 'expires-ms', 'expires-iso', 'refresh-version', 'source', 'auth-type'])
@pytest.mark.parametrize('phase', ['preflight', 'postflight'])
def test_real_selected_disk_authority_contract(monkeypatch, tmp_path, change, phase):
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'profile'))
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a')])
    assert pool.select().id == 'a'
    owner = pool.current()
    home = tmp_path / 'profile'
    home.mkdir(exist_ok=True)
    file = home / 'auth.json'
    disk = owner.to_dict()
    # Missing optional endpoint/expiry is the actual serialized schema, not
    # permission to ignore explicitly removed mandatory credential material.
    assert 'base_url' not in disk and 'expires_at_ms' not in disk
    def write(changed):
        row = dict(disk)
        if changed == 'token-missing': row.pop('access_token')
        elif changed == 'token-empty': row['access_token'] = ''
        elif changed == 'token-other': row['access_token'] = 'synthetic-replacement'
        elif changed == 'endpoint': row['base_url'] = 'https://other.invalid/tenant/v1'
        elif changed == 'expires-ms': row['expires_at_ms'] = 1
        elif changed == 'expires-iso': row['expires_at'] = '2000-01-01T00:00:00Z'
        elif changed == 'refresh-version': row['last_refresh'] = '2099-01-01T00:00:00Z'
        elif changed == 'source': row['source'] = 'manual:replacement'
        elif changed == 'auth-type': row['auth_type'] = 'api_key'
        file.write_text(json.dumps({'credential_pool': {'openai-codex': [row]}}))
    write(change if phase == 'preflight' else 'unchanged')
    original = aux._openai_http_client_kwargs
    def respond(req):
        requests.append(('inference', str(req.url), req.headers.get('chatgpt-account-id')))
        if phase == 'postflight': write(change)
        return response_for('openai-codex')
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', lambda *a, **kw:
        {'http_client': httpx.AsyncClient(transport=httpx.MockTransport(respond))}
        if kw.get('async_mode') else original(*a, **kw))
    if change == 'unchanged':
        assert run(config).text == 'synthetic'
        assert len(requests) == 1
    else:
        with pytest.raises(CompletionHeld, match='credential_authority|credential_expired'):
            run(config)
        assert len(requests) == (0 if phase == 'preflight' else 1)
    assert pool.current() is owner


@pytest.mark.parametrize('change', ['unchanged', 'profile-home', 'source-store', 'store-version', 'unrelated-owner'])
def test_selected_authority_binds_persisted_source_provenance(monkeypatch, tmp_path, change):
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    profile, other = tmp_path / 'profile', tmp_path / 'other'
    profile.mkdir(); other.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(profile))
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a')])
    assert pool.select().id == 'a'
    payload = {'version': 1, 'credential_pool': {'openai-codex': [pool.current().to_dict()]}}
    file = profile / 'auth.json'
    file.write_text(json.dumps(payload))
    (other / 'auth.json').write_text(json.dumps(payload))
    global_file = tmp_path / 'global-auth.json'
    global_file.write_text(json.dumps(payload))
    monkeypatch.setattr('clover_cli.auth._global_auth_file_path', lambda: global_file)
    original = aux._openai_http_client_kwargs
    def respond(req):
        requests.append(req)
        if change == 'profile-home': monkeypatch.setenv('CLOVER_HOME', str(other))
        elif change == 'source-store': file.write_text(json.dumps({'credential_pool': {}}))
        elif change == 'store-version':
            payload['version'] = 99
            file.write_text(json.dumps(payload))
        elif change == 'unrelated-owner':
            payload['credential_pool']['openai-codex'].append(entry('other').to_dict())
            file.write_text(json.dumps(payload))
        return response_for('openai-codex')
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', lambda *a, **kw:
        {'http_client': httpx.AsyncClient(transport=httpx.MockTransport(respond))}
        if kw.get('async_mode') else original(*a, **kw))
    if change in {'unchanged', 'unrelated-owner'}:
        assert run(config).text == 'synthetic'
    else:
        with pytest.raises(CompletionHeld, match='authority_changed'): run(config)
    assert len(requests) == 1


@pytest.mark.parametrize('name', ['beans', 'custom', 'openai', 'openai-api', 'openai-codex', 'codex',
                                  'gemini', 'anthropic', 'deepseek', 'openrouter'])
@pytest.mark.parametrize('base', ['http://127.0.0.1:8080/v1/',
    'https://unrelated.invalid:443/tenant-b/v1', 'https://api.openai.com/tenant-b/v1/'])
@pytest.mark.parametrize('source', ['keyless', 'task', 'entry-env'])
def test_configured_alias_credential_policy_owns_effective_wire(monkeypatch, name, base, source):
    import contextvars
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    config['model'] = {'provider': name, 'default':'main'}
    config['providers'] = {name: {'base_url': base}}
    task = config['auxiliary']['presence'] = {'provider':name, 'model':'literal/model:unmodified'}
    seen = []
    def read(env):
        seen.append(env)
        return 'synthetic-explicit-owner' if env == 'DECLARED_KEY' else 'synthetic-unrelated-service-key'
    monkeypatch.setattr(aux, '_scoped_key_env', read)
    monkeypatch.setattr('clover_cli.config.get_env_value_prefer_dotenv', read)
    monkeypatch.setattr(aux, '_RUNTIME_MAIN_CONTEXT', contextvars.ContextVar('alias-runtime', default={}))
    if source == 'task': task['api_key'] = 'synthetic-explicit-owner'
    elif source == 'entry-env': config['providers'][name]['key_env'] = 'DECLARED_KEY'
    assert run(config).text == 'synthetic'
    assert len(requests) == 1
    request = requests[0]
    assert json.loads(request.content)['model'] == 'literal/model:unmodified'
    assert request.url == httpx.URL(base.rstrip('/') + '/chat/completions')
    # The SDK may omit the default port on the wire; the configured token and
    # literal model remain unchanged, and endpoint ownership is structural.
    assert request.headers['authorization'] == ('Bearer no-key-required' if source == 'keyless'
                                               else 'Bearer synthetic-explicit-owner')
    assert set(seen) <= {'DECLARED_KEY'}
    assert 'chatgpt-account-id' not in request.headers


@pytest.mark.parametrize('name', ['openai', 'gemini', 'anthropic'])
def test_configured_alias_does_not_borrow_builtin_profile_headers(monkeypatch, name):
    from types import SimpleNamespace
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    config['providers'] = {name:{'base_url':'https://other.invalid/v1'}}
    config['auxiliary']['presence'] = {'provider':name,'model':'literal','api_key':'synthetic-task'}
    monkeypatch.setattr('providers.get_provider_profile', lambda *a: SimpleNamespace(
        default_headers={'X-Service-Secret':'synthetic-unrelated-profile'}))
    assert run(config).text == 'synthetic'
    assert len(requests) == 1 and 'x-service-secret' not in requests[0].headers


@pytest.mark.parametrize('name', ['gemini', 'anthropic', 'openai-api'])
def test_alias_runtime_without_concrete_endpoint_is_not_an_owner(monkeypatch, name):
    import contextvars
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    config['providers'] = {name:{'base_url':'https://other.invalid/v1'}}
    config['auxiliary']['presence'] = {'provider':name,'model':'literal'}
    monkeypatch.setattr(aux, '_RUNTIME_MAIN_CONTEXT', contextvars.ContextVar('alias-runtime', default={
        'provider':name, 'model':'main', 'api_key':'synthetic-unrelated-runtime'}))
    monkeypatch.setattr(aux,'_scoped_key_env',lambda *a:None)
    monkeypatch.setattr('clover_cli.config.get_env_value_prefer_dotenv',lambda *a:None)
    assert run(config).text == 'synthetic'
    assert len(requests) == 1 and requests[0].headers['authorization'] == 'Bearer no-key-required'


@pytest.mark.parametrize('provider,env,keyenv,key', [
    ('openai-api','OPENAI_BASE_URL','OPENAI_API_KEY','sk-synthetic-unrelated'),
    ('deepseek','DEEPSEEK_BASE_URL','DEEPSEEK_API_KEY','sk-synthetic-unrelated'),
])
def test_builtin_endpoint_environment_does_not_reassign_service_secret(monkeypatch, provider, env, keyenv, key):
    requests = []
    response_provider = 'openai-api' if provider == 'openai-api' else 'openai'
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for(response_provider))
    config['auxiliary']['presence'] = {'provider':provider,'model':'literal'}
    monkeypatch.setenv(env, 'https://unrelated.invalid/tenant/v1')
    monkeypatch.setattr('clover_cli.config.get_env_value_prefer_dotenv',lambda name:key if name == keyenv else None)
    with pytest.raises(CompletionHeld, match='credential_endpoint_unowned'): run(config)
    assert not requests
    config['auxiliary']['presence']['api_key'] = 'synthetic-explicit-owner'
    # Explicit task policy owns this endpoint independently of ambient service.
    response_provider = 'openai-api' if provider == 'openai-api' else 'openai'
    original = aux._openai_http_client_kwargs
    monkeypatch.setattr(aux,'_openai_http_client_kwargs', lambda *a,**kw:
        {'http_client': (httpx.AsyncClient if kw.get('async_mode') else httpx.Client)(
            transport=httpx.MockTransport(lambda req: requests.append(req) or response_for(response_provider)))})
    assert run(config).text == 'synthetic'
    assert len(requests) == 1
    assert 'synthetic-explicit-owner' in str(dict(requests[0].headers))


@pytest.mark.parametrize('name', ['openai', 'anthropic', 'gemini', 'openai-codex'])
@pytest.mark.parametrize('wire', ['chat_completions','codex_responses','anthropic_messages'])
@pytest.mark.parametrize('source', ['keyless','task'])
def test_configured_name_policy_applies_before_every_native_serializer(monkeypatch, name, wire, source):
    response_provider = {'chat_completions':'openai','codex_responses':'openai-api',
                         'anthropic_messages':'anthropic'}[wire]
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for(response_provider))
    config['providers'] = {name:{'base_url':'https://unrelated.invalid:443/tenant/v1/', 'api_mode':wire}}
    task = config['auxiliary']['presence'] = {'provider':name,'model':'literal'}
    if source == 'task': task['api_key'] = 'synthetic-explicit-owner'
    def read(env): pytest.fail('configured policy read unrelated secret: '+env)
    monkeypatch.setattr(aux,'_scoped_key_env',read)
    monkeypatch.setattr('clover_cli.config.get_env_value_prefer_dotenv',read)
    assert run(config,timeout=2).text == 'synthetic'
    assert len(requests) == 1
    headers = requests[0].headers
    assert headers.get('authorization',headers.get('x-api-key')) in {
        'Bearer no-key-required','no-key-required'} if source == 'keyless' else (
        headers.get('authorization',headers.get('x-api-key')) in {
            'Bearer synthetic-explicit-owner','synthetic-explicit-owner'})
    assert 'chatgpt-account-id' not in headers


@pytest.mark.parametrize('expiry', ['expires_at', 'expires_at_ms'])
def test_persisted_expiry_is_current_again_at_publication(monkeypatch, tmp_path, expiry):
    from datetime import datetime, timezone
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'profile'))
    owner = entry('a')
    now = time.time()
    until = now + 1
    monkeypatch.setattr('agent.bounded_auth.time.time', lambda: now)
    setattr(owner, expiry, datetime.fromtimestamp(until, timezone.utc).isoformat()
            if expiry == 'expires_at' else int(until * 1000))
    config, pool, requests = genuine_pool_route(monkeypatch, [owner])
    assert pool.select().id == 'a'
    file = tmp_path / 'profile' / 'auth.json'
    file.parent.mkdir(exist_ok=True)
    file.write_text(json.dumps({'credential_pool':{'openai-codex':[owner.to_dict()]}}))
    async def respond(req):
        requests.append(req)
        monkeypatch.setattr('agent.bounded_auth.time.time', lambda: now + 2)
        return response_for('openai-codex')
    original = aux._openai_http_client_kwargs
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', lambda *a, **kw:
        {'http_client': httpx.AsyncClient(transport=httpx.MockTransport(respond))}
        if kw.get('async_mode') else original(*a, **kw))
    with pytest.raises(CompletionHeld, match='credential_expired'): run(config)
    assert len(requests) == 1


@pytest.mark.parametrize('source', ['manual', 'env:SYNTHETIC_OWNED'])
def test_real_disk_owned_and_borrowed_optional_field_schema_positive(monkeypatch, tmp_path, source):
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'profile'))
    owner = entry('a'); owner.source = source
    config, pool, requests = genuine_pool_route(monkeypatch, [owner])
    assert pool.select().id == 'a'
    file = tmp_path / 'profile' / 'auth.json'
    file.parent.mkdir(exist_ok=True)
    row = owner.to_dict()
    assert ('access_token' in row) == (source == 'manual')
    file.write_text(json.dumps({'credential_pool':{'openai-codex':[row]}}))
    assert run(config).text == 'synthetic' and len(requests) == 1


def test_corrupt_selected_store_holds_without_backup_or_other_disk_write(monkeypatch, tmp_path):
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'profile'))
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a')])
    assert pool.select().id == 'a'
    file = tmp_path / 'profile' / 'auth.json'
    file.parent.mkdir(exist_ok=True)
    file.write_text('not-json-synthetic')
    before = {p.name:p.read_bytes() for p in file.parent.iterdir() if p.is_file()}
    with pytest.raises(CompletionHeld, match='credential_authority_unavailable'): run(config)
    assert not requests
    assert {p.name:p.read_bytes() for p in file.parent.iterdir() if p.is_file()} == before


def test_selected_runtime_change_during_disk_read_is_not_baselined(monkeypatch, tmp_path):
    from agent import bounded_auth
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'profile'))
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a')])
    assert pool.select().id == 'a'
    file = tmp_path / 'profile' / 'auth.json'
    file.parent.mkdir(exist_ok=True)
    file.write_text(json.dumps({'credential_pool':{'openai-codex':[pool.current().to_dict()]}}))
    original = bounded_auth._pool_disk_state
    def mutate(*args):
        result = original(*args)
        aux._RUNTIME_MAIN_CONTEXT.get()['base_url'] = 'https://new-owner.invalid/v1'
        return result
    monkeypatch.setattr(bounded_auth, '_pool_disk_state', mutate)
    with pytest.raises(CompletionHeld, match='credential_authority_changed'): run(config)
    assert not requests


def test_profile_selected_owner_ignores_unrelated_corrupt_global_store(monkeypatch, tmp_path):
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'profile'))
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a')])
    assert pool.select().id == 'a'
    file = tmp_path / 'profile' / 'auth.json'
    file.parent.mkdir(exist_ok=True)
    file.write_text(json.dumps({'credential_pool':{'openai-codex':[pool.current().to_dict()]}}))
    global_file = tmp_path / 'global-auth.json'
    global_file.write_text('unrelated-malformed-store')
    monkeypatch.setattr('clover_cli.auth._global_auth_file_path',lambda:global_file)
    assert run(config).text == 'synthetic' and len(requests) == 1
    assert global_file.read_text() == 'unrelated-malformed-store'
    assert not global_file.with_suffix('.json.corrupt').exists()


@pytest.mark.parametrize('change', ['unchanged','file-removed','source-replaced','version-changed'])
def test_genuinely_loaded_main_owner_retains_original_persisted_source(monkeypatch, tmp_path, change):
    from agent.credential_pool import load_pool
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'profile'))
    config, _, requests = genuine_pool_route(monkeypatch, [entry('a')])
    monkeypatch.setattr('clover_cli.config.load_config_readonly',lambda:config)
    file = tmp_path / 'profile' / 'auth.json'
    file.parent.mkdir(exist_ok=True)
    payload = {'version':1,'credential_pool':{'openai-codex':[entry('a').to_dict()]}}
    file.write_text(json.dumps(payload))
    pool = load_pool('openai-codex')
    assert pool.select().id == 'a'
    aux._RUNTIME_MAIN_CONTEXT.get()['credential_pool'] = pool
    if change == 'file-removed': file.unlink()
    elif change == 'source-replaced':
        global_file = tmp_path / 'global-auth.json'
        global_file.write_text(json.dumps(payload))
        monkeypatch.setattr('clover_cli.auth._global_auth_file_path',lambda:global_file)
        file.write_text(json.dumps({'credential_pool':{}}))
    elif change == 'version-changed':
        payload['version'] = 99
        file.write_text(json.dumps(payload))
    if change == 'unchanged':
        assert run(config).text == 'synthetic' and len(requests) == 1
    else:
        with pytest.raises(CompletionHeld,match='credential_authority_changed|credential_authority_revoked'):
            run(config)
        assert not requests



def test_legitimate_main_persistence_can_move_global_owner_into_profile(monkeypatch, tmp_path):
    from agent.credential_pool import load_pool
    from tests.agent.test_provider_security_corrected import genuine_pool_route, entry
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'profile'))
    config, _, requests = genuine_pool_route(monkeypatch, [entry('a')])
    monkeypatch.setattr('clover_cli.config.load_config_readonly',lambda:config)
    file = tmp_path / 'global-auth.json'
    file.write_text(json.dumps({'version':1,'credential_pool':{'openai-codex':[entry('a').to_dict()]}}))
    monkeypatch.setattr('clover_cli.auth._global_auth_file_path',lambda:file)
    pool = load_pool('openai-codex')
    assert pool.select().id == 'a'
    assert pool._credential_source[0] == str(file.resolve())
    # Ordinary main persistence/refresh remains allowed. Only bounded presence
    # must refuse to change or rotate the authority while it is assessing.
    pool._persist()
    assert pool._credential_source[0] == str((tmp_path/'profile'/'auth.json').resolve())
    aux._RUNTIME_MAIN_CONTEXT.get()['credential_pool'] = pool
    assert run(config).text == 'synthetic' and len(requests) == 1

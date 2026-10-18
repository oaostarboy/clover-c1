"""Independent contract attacks; synthetic tokens/HTTP only, never live auth."""
import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest

from agent import auxiliary_client as aux
from agent.bounded_completion import complete, CompletionHeld
from agent.credential_pool import CredentialPool, PooledCredential, STRATEGY_ROUND_ROBIN
from tests.agent.test_presence_provider_wire import route_fixture as legacy_route_fixture, response_for

def route_fixture(monkeypatch, provider, respond):
    config = legacy_route_fixture(monkeypatch, provider, respond)
    if provider == "openai-codex":
        config["auxiliary"]["presence"]["api_key"] = jwt("synthetic-account")
    return config


def jwt(account, expired=False):
    body = {'exp': int(time.time()) + (-1000 if expired else 10000),
            'https://api.openai.com/auth': {'chatgpt_account_id': account}}
    return 'e30.' + base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=') + '.synthetic'


def entry(name, expired=False, priority=0):
    return PooledCredential(provider='openai-codex', id=name, label=name, auth_type='oauth',
        priority=priority, source='manual', access_token=jwt(name, expired),
        refresh_token='synthetic-refresh-' + name)


def genuine_pool_route(monkeypatch, entries):
    original = aux._select_pool_entry
    requests = []
    def infer(request):
        requests.append(('inference', str(request.url), request.headers.get('chatgpt-account-id')))
        return response_for('openai-codex')
    config = route_fixture(monkeypatch, 'openai-codex', infer)
    monkeypatch.setattr(aux, '_select_pool_entry', original)
    pool = CredentialPool('openai-codex', entries)
    monkeypatch.setattr(pool, '_persist', lambda **kw: None)
    config['auxiliary']['presence'].pop('api_key', None)
    monkeypatch.setattr(aux, 'load_pool', lambda provider: pytest.fail('bounded path loaded mutable pool'))
    import contextvars
    authority = contextvars.ContextVar('test_runtime', default={
        'provider': 'openai-codex', 'model': config['auxiliary']['presence']['model'],
        'base_url': aux._CODEX_AUX_BASE_URL, 'credential_pool': pool})
    monkeypatch.setattr(aux, '_RUNTIME_MAIN_CONTEXT', authority)
    def send(client, request, **kwargs):
        requests.append(('auth', str(request.url), None))
        return httpx.Response(200, request=request, json={'access_token': jwt('refreshed'),
            'refresh_token': 'synthetic-refreshed', 'expires_in': 3600})
    monkeypatch.setattr(httpx.Client, 'send', send)
    return config, pool, requests


def run(config, timeout=1):
    return asyncio.run(complete(config=config, messages=[{'role': 'user', 'content': 'synthetic'}],
                               max_output_tokens=64, timeout=timeout))


def test_one_physical_request_includes_auth_not_only_inference(monkeypatch):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('selected'), entry('other', True, 1)])
    # Establish actual main selection before introducing the stale sibling.
    sibling = pool._entries.pop()
    assert pool.select().id == 'selected'
    pool._entries.append(sibling)
    result = run(config)
    print('AUDIT_AUTH_REQUESTS', json.dumps(requests), 'DECLARED', result.limits.max_physical_requests)
    assert len(requests) <= result.limits.max_physical_requests


def test_all_stale_accounts_cannot_refresh_then_make_inference(monkeypatch):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a', True), entry('b', True, 1)])
    pool._current_id = 'a'
    before = repr(pool._entries)
    with pytest.raises(CompletionHeld, match='credential_expired'):
        run(config)
    assert not requests and repr(pool._entries) == before and pool._current_id == 'a'


def test_selected_account_is_not_rotated_by_presence(monkeypatch):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a'), entry('b', priority=1)])
    pool._strategy = STRATEGY_ROUND_ROBIN
    main_selected = pool.select()
    assert main_selected.id == 'a'
    result = run(config)
    print('AUDIT_ACCOUNT', main_selected.id, requests[-1][2], 'CURRENT', pool.current().id)
    assert requests[-1][2] == main_selected.id


def test_explicit_codex_task_key_does_not_read_ambient_account(monkeypatch):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('ambient')])
    config['auxiliary']['presence']['api_key'] = jwt('task-owned')
    result = run(config)
    print('AUDIT_EXPLICIT_ACCOUNT', requests[-1][2])
    assert requests[-1][2] == 'task-owned'


def test_already_cancelled_task_does_not_send_auth_refresh(monkeypatch):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('stale', True)])
    async def cancelled():
        asyncio.current_task().cancel()
        await complete(config=config, messages=[{'role': 'user', 'content': 'synthetic'}],
                       max_output_tokens=64, timeout=1)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled())
    print('AUDIT_PRE_CANCEL', json.dumps(requests))
    assert not requests


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'gemini', 'openai-api', 'openai-codex'])
@pytest.mark.parametrize('value', [-1, True, '8', 1.5])
def test_malformed_numeric_usage_never_becomes_known(monkeypatch, provider, value):
    def respond(request):
        response = response_for(provider)
        if provider in {'openai-api', 'openai-codex'}:
            event = json.loads(response.content.decode().split('data: ')[1].strip())
            event['response']['usage'] = {'output_tokens': value}
            return httpx.Response(200, content='data: ' + json.dumps(event) + '\n\n')
        body = response.json()
        if provider == 'openai': body['usage'] = {'completion_tokens': value}
        elif provider == 'anthropic': body['usage'] = {'output_tokens': value}
        else: body['usageMetadata'] = {'candidatesTokenCount': value, 'thoughtsTokenCount': 0}
        return httpx.Response(200, json=body)
    config = route_fixture(monkeypatch, provider, respond)
    result = run(config)
    assert result.usage == 'unknown' and result.output_tokens is None


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'gemini', 'openai-api', 'openai-codex'])
def test_invalid_response_never_returns_success(monkeypatch, provider):
    config = route_fixture(monkeypatch, provider, lambda req: httpx.Response(200, content=b'not json'))
    with pytest.raises((ValueError, KeyError, TypeError, AttributeError)):
        run(config)


def test_custom_task_never_borrows_different_main_provider_secret(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        return response_for('openai')
    config = route_fixture(monkeypatch, 'openai', respond)
    monkeypatch.setattr(aux, '_scoped_key_env', lambda key: None)
    config['model'] = {'provider': 'anthropic', 'default': 'main-model',
                       'api_key': 'synthetic-main-anthropic-secret'}
    config['auxiliary']['presence'] = {'provider': 'custom', 'model': 'task-model',
        'base_url': 'https://different-owner.invalid/v1'}
    with pytest.raises(CompletionHeld, match='credential_endpoint_unowned'):
        run(config)
    assert not requests


def test_auth_owner_change_during_http_must_hold_publication(monkeypatch):
    from dataclasses import replace
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('original')])
    pool._current_id = 'original'
    monkeypatch.setattr('clover_cli.config.load_config', lambda: config)
    def infer(request):
        requests.append(('inference', str(request.url), request.headers.get('chatgpt-account-id')))
        pool._entries = [replace(pool._entries[0], access_token=jwt('new-owner'))]
        return response_for('openai-codex')
    original_factory = aux._openai_http_client_kwargs
    def factory(*args, **kwargs):
        if kwargs.get('async_mode'):
            return {'http_client': httpx.AsyncClient(transport=httpx.MockTransport(infer))}
        return original_factory(*args, **kwargs)
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', factory)
    with pytest.raises(CompletionHeld, match='changed|owner|account'):
        asyncio.run(aux.presence_completion(messages=[{'role': 'user', 'content': 'synthetic'}],
            max_output_tokens=64, timeout=1))
    print('AUDIT_OWNER_RACE', json.dumps(requests))


def test_exhausted_account_does_not_run_a_quota_discovery_request(monkeypatch):
    exhausted = entry('quota')
    exhausted.last_status = 'exhausted'
    exhausted.last_error_code = 429
    exhausted.last_error_reason = 'rate_limit_exceeded'
    exhausted.last_status_at = time.time()
    exhausted.last_error_reset_at = time.time() + 3600
    config, pool, requests = genuine_pool_route(monkeypatch, [exhausted])
    def send(client, request, **kwargs):
        requests.append(('quota-probe', str(request.url), None))
        return httpx.Response(200, request=request, json={'rate_limit': {'primary_window': {'used_percent': 1}}})
    monkeypatch.setattr(httpx.Client, 'send', send)
    pool._current_id = 'quota'
    before = repr(pool._entries)
    with pytest.raises(CompletionHeld, match='credential_unavailable'):
        run(config)
    assert not requests and repr(pool._entries) == before and pool._current_id == 'quota'


def test_named_custom_local_owner_uses_existing_mapping(monkeypatch):
    from agent.bounded_completion import admitted, select_route
    config = {'model': {'provider': 'beans', 'default': 'local-model'},
        'presence': {'budget_mode': 'requests'},
        'custom_providers': [{'name': 'beans', 'base_url': 'http://localhost:8080/v1'}]}
    monkeypatch.setattr('clover_cli.config.load_config_readonly', lambda: config)
    cap = admitted(select_route(config))
    assert cap.transport == 'chat_completions'


def test_same_provider_explicit_task_inherits_main_wire(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        return response_for('openai-api')
    config = route_fixture(monkeypatch, 'openai', respond)
    config['model'] = {'provider': 'custom', 'default': 'configured',
        'base_url': 'https://chosen.invalid/v1', 'api_mode': 'codex_responses', 'api_key': 'synthetic-configured-key'}
    config['auxiliary']['presence'] = {'provider': 'custom', 'model': 'configured'}
    result = run(config)
    print('AUDIT_WIRE_INHERITANCE', str(requests[0].url), result.route.api_mode)
    assert str(requests[0].url).endswith('/responses')


@pytest.mark.parametrize('field', ['max_calls_per_event', 'max_calls_per_day', 'max_output_tokens'])
def test_zero_budget_actual_gateway_never_reaches_auth(monkeypatch, tmp_path, field):
    import yaml
    from tests.gateway.test_presence_integration import setup_host
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    config = yaml.safe_load((h.home / 'config.yaml').read_text())
    config['presence']['budget_mode'] = 'requests'
    config['proactive']['aux'] = {'max_calls_per_event': 1, 'max_calls_per_day': 1,
                                 'max_output_tokens': 64}
    config['proactive']['aux'][field] = 0
    (h.home / 'config.yaml').write_text(yaml.safe_dump(config))
    monkeypatch.setattr(aux, 'resolve_provider_client', lambda *a, **kw: pytest.fail('zero budget reached auth'))
    try:
        h.inbound_human('I am frustrated. Please be brief.', message_id=211)
        assert not requests and len(h.scripted.requests) == 1
    finally:
        h.close()


def test_failed_request_keeps_low_daily_reservation_in_actual_gateway(monkeypatch, tmp_path):
    import yaml
    import sqlite3
    from tests.gateway.test_presence_integration import setup_host
    from cron.presence import ThoughtStore
    original = aux.resolve_provider_client
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    monkeypatch.setattr(aux, 'resolve_provider_client', original)
    physical = []
    def fail(request):
        physical.append(str(request.url))
        with sqlite3.connect(ThoughtStore().path) as db:
            assert db.execute('SELECT COUNT(*) FROM presence_budget').fetchone()[0] == 1
        return httpx.Response(500, json={'error': {'message': 'synthetic'}})
    native = route_fixture(monkeypatch, 'openai', fail)
    config = yaml.safe_load((h.home / 'config.yaml').read_text())
    config['presence'].update(native['presence'])
    config['auxiliary'] = native['auxiliary']
    config['proactive']['aux'] = {'max_calls_per_event': 1, 'max_calls_per_day': 1,
                                 'max_output_tokens': 64}
    (h.home / 'config.yaml').write_text(yaml.safe_dump(config))
    try:
        h.inbound_human('I am frustrated. Please be brief.', message_id=212)
        h.inbound_human('I am frustrated. Please be brief again.', message_id=213)
        assert len(physical) == 1 and len(h.scripted.requests) == 2
        with sqlite3.connect(ThoughtStore().path) as db:
            assert db.execute('SELECT COUNT(*) FROM presence_budget').fetchone()[0] == 1
    finally:
        h.close()


def test_concurrent_surface_reservations_share_one_profile_quota(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from cron.presence import ThoughtStore
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'shared-profile'))
    def reserve(index):
        surface = 'telegram' if index % 2 else 'desktop'
        return ThoughtStore().reserve_assessment(event_id=f'{surface}:{index}',
            thought_id=f'thought-{index}', revision=1, day='synthetic-day', max_calls=1, tokens=64)
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(reserve, range(8)))
    assert sum(results) == 1
    assert ThoughtStore().assessment_count('synthetic-day') == 1
    assert not ThoughtStore().reserve_assessment(event_id='retry', thought_id='retry',
        revision=1, day='synthetic-day', max_calls=1, tokens=64)


def test_source_pinned_imports():
    import agent.bounded_completion
    import agent.bounded_auth
    import agent.bounded_provider_metadata
    import agent.turn_context
    import clover_cli.auth
    import cron.presence
    root = Path(__file__).resolve().parents[2]
    for module in [aux, agent.bounded_completion, clover_cli.auth, cron.presence,
                   agent.bounded_auth, agent.bounded_provider_metadata, agent.turn_context]:
        assert Path(module.__file__).resolve().is_relative_to(root), module.__file__
    print('AUDIT_ORIGINS', json.dumps({module.__name__: module.__file__ for module in
          [aux, agent.bounded_completion, clover_cli.auth, cron.presence,
                   agent.bounded_auth, agent.bounded_provider_metadata, agent.turn_context]}))


def test_missing_current_authority_holds_without_guess_or_mutation(monkeypatch):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a'), entry('b')])
    before = repr(pool._entries)
    with pytest.raises(CompletionHeld, match='selected_authority_missing'):
        run(config)
    assert not requests and pool.current() is None and repr(pool._entries) == before


@pytest.mark.parametrize('source', ['api_key', 'key_env'])
def test_explicit_task_credential_is_config_authority_not_pool_membership(monkeypatch, source):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('ambient')])
    monkeypatch.setattr(pool, 'current', lambda: pytest.fail('explicit task consulted ambient account'))
    key = jwt('config-owned')
    if source == 'api_key':
        config['auxiliary']['presence']['api_key'] = key
    else:
        config['auxiliary']['presence']['key_env'] = 'OWNED_TASK_KEY'
        monkeypatch.setattr(aux, '_scoped_key_env', lambda name: key if name == 'OWNED_TASK_KEY' else None)
    assert run(config).text == 'synthetic'
    assert requests == [('inference', aux._CODEX_AUX_BASE_URL + '/responses', 'config-owned')]


@pytest.mark.parametrize('provider', ['custom', 'openai', 'openai-api', 'gemini'])
def test_same_provider_foreign_endpoint_does_not_inherit_secret(monkeypatch, provider):
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    config['model'] = {'provider': provider, 'default': 'chosen',
        'base_url': 'https://owner.invalid/tenant-a/v1', 'api_key': 'synthetic-main-secret'}
    config['auxiliary']['presence'] = {'provider': provider, 'model': 'chosen',
        'base_url': 'https://owner.invalid/tenant-b/v1'}
    with pytest.raises(CompletionHeld, match='credential_endpoint_unowned'):
        run(config)
    assert not requests


@pytest.mark.parametrize('source', ['task', 'main', 'named', 'environment'])
def test_exact_configured_endpoint_credential_positive(monkeypatch, source):
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    target = 'https://owner.invalid/tenant-a/v1'
    task = {'provider': 'custom', 'model': 'chosen', 'base_url': target}
    config['auxiliary']['presence'] = task
    if source == 'task':
        task['api_key'] = 'synthetic-owned'
    elif source == 'main':
        config['model'] = {'provider': 'custom', 'default': 'chosen', 'base_url': target, 'api_key': 'synthetic-owned'}
    elif source == 'named':
        task['provider'] = 'beans'
        config['custom_providers'] = [{'name': 'beans', 'base_url': target, 'api_key': 'synthetic-owned'}]
    else:
        config['model'] = {'provider': 'custom', 'default': 'chosen', 'base_url': target}
        monkeypatch.setattr(aux, '_scoped_key_env', lambda name: 'synthetic-owned' if name == 'OPENAI_API_KEY' else None)
    assert run(config).text == 'synthetic'
    assert len(requests) == 1 and str(requests[0].url) == target + '/chat/completions'
    assert requests[0].headers['authorization'] == 'Bearer synthetic-owned'


def test_named_custom_local_genuine_wire_is_not_only_metadata(monkeypatch):
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    config['auxiliary']['presence'] = {'provider': 'beans', 'model': 'chosen'}
    config['custom_providers'] = [{'name': 'beans', 'base_url': 'http://localhost:8080/v1'}]
    assert run(config).text == 'synthetic'
    assert len(requests) == 1 and str(requests[0].url) == 'http://localhost:8080/v1/chat/completions'
    assert requests[0].headers['authorization'] == 'Bearer no-key-required'


@pytest.mark.parametrize('change', ['remove', 'status', 'selection', 'endpoint'])
def test_selected_authority_revalidated_after_http(monkeypatch, change):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a'), entry('b')])
    pool._current_id = 'a'
    def respond(request):
        requests.append(('inference', str(request.url), request.headers.get('chatgpt-account-id')))
        if change == 'remove': pool._entries = []
        elif change == 'status': pool.current().last_status = 'dead'
        elif change == 'selection': pool._current_id = 'b'
        else: pool.current().base_url = 'https://foreign.invalid/v1'
        return response_for('openai-codex')
    original = aux._openai_http_client_kwargs
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', lambda *a, **kw:
        {'http_client': httpx.AsyncClient(transport=httpx.MockTransport(respond))} if kw.get('async_mode') else original(*a, **kw))
    with pytest.raises(CompletionHeld, match='authority_changed'):
        run(config)
    assert len(requests) == 1


def test_precancel_does_not_even_acquire_credentials(monkeypatch):
    config = route_fixture(monkeypatch, 'openai', lambda req: pytest.fail('sent'))
    monkeypatch.setattr(aux, 'resolve_provider_client', lambda *a, **kw: pytest.fail('acquired'))
    async def cancelled():
        asyncio.current_task().cancel()
        await complete(config=config, messages=[{'role':'user','content':'synthetic'}], max_output_tokens=64, timeout=1)
    with pytest.raises(asyncio.CancelledError): asyncio.run(cancelled())


def test_foreign_main_extra_headers_cannot_replace_owned_task_credential(monkeypatch):
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    config['model'] = {'provider': 'anthropic', 'default': 'main',
        'default_headers': {'Authorization': 'Bearer synthetic-foreign-header', 'X-Foreign-Secret': 'synthetic-foreign-secret'}}
    config['auxiliary']['presence'] = {'provider':'custom','model':'chosen',
        'base_url':'https://owned.invalid/v1','api_key':'synthetic-owned'}
    monkeypatch.setattr('clover_cli.config.load_config', lambda: config)
    assert run(config).text == 'synthetic'
    assert requests[0].headers['authorization'] == 'Bearer synthetic-owned'
    assert 'x-foreign-secret' not in requests[0].headers


def test_acquisition_deadline_overrun_never_enters_http(monkeypatch):
    from agent import bounded_auth
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    original = bounded_auth.acquire
    def slow(*a, **kw):
        time.sleep(.03)
        return original(*a, **kw)
    monkeypatch.setattr(bounded_auth, 'acquire', slow)
    with pytest.raises(TimeoutError): run(config, timeout=.01)
    assert not requests


@pytest.mark.parametrize('name', ['openai', 'gemini', 'codex', 'anthropic'])
def test_user_provider_mapping_precedes_builtin_transport_and_auth(monkeypatch, name):
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    config['providers'] = {name: {'base_url':'http://localhost:8080/v1', 'api_key':'synthetic-user-key'}}
    config['auxiliary']['presence'] = {'provider': name, 'model':'exact-local-model'}
    assert run(config).text == 'synthetic'
    assert len(requests) == 1 and str(requests[0].url) == 'http://localhost:8080/v1/chat/completions'
    assert requests[0].headers['authorization'] == 'Bearer synthetic-user-key'
    assert 'chatgpt-account-id' not in requests[0].headers


def test_configured_key_cannot_change_during_acquisition_and_be_baselined(monkeypatch):
    from agent import bounded_auth
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    task = config['auxiliary']['presence']
    task['key_env'] = 'OWNED_TASK'
    reads = []
    def key(name):
        reads.append(name)
        return 'synthetic-first' if len(reads) == 1 else 'synthetic-replacement'
    monkeypatch.setattr(aux, '_scoped_key_env', key)
    with pytest.raises(CompletionHeld, match='authority_changed'):
        run(config)
    assert not requests


def test_selected_pool_external_revocation_after_http_holds(monkeypatch):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a')])
    pool._current_id = 'a'
    disk = [pool.current().to_dict()]
    monkeypatch.setattr('clover_cli.auth.read_credential_pool', lambda provider: disk)
    original = aux._openai_http_client_kwargs
    def respond(request):
        requests.append(('inference', str(request.url), request.headers.get('chatgpt-account-id')))
        disk.clear()
        return response_for('openai-codex')
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', lambda *a, **kw:
        {'http_client': httpx.AsyncClient(transport=httpx.MockTransport(respond))} if kw.get('async_mode') else original(*a, **kw))
    with pytest.raises(CompletionHeld, match='authority_changed'): run(config)
    assert len(requests) == 1 and pool.current().id == 'a'


def test_selected_pool_revoked_before_acquisition_does_not_send(monkeypatch, tmp_path):
    from clover_constants import get_clover_home
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a')])
    pool._current_id = 'a'
    home = get_clover_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'auth.json').write_text(json.dumps({'credential_pool': {'openai-codex': []}}))
    with pytest.raises(CompletionHeld, match='authority_revoked'): run(config)
    assert not requests


def test_precancel_presence_entry_does_not_read_config(monkeypatch):
    monkeypatch.setattr('clover_cli.config.load_config', lambda: pytest.fail('precancel read config'))
    async def cancelled():
        asyncio.current_task().cancel()
        await aux.presence_completion(messages=[{'role':'user','content':'synthetic'}], max_output_tokens=64, timeout=1)
    with pytest.raises(asyncio.CancelledError): asyncio.run(cancelled())


@pytest.mark.parametrize('provider,model,base', [
    ('opencode-free', 'exact-free-model', 'https://opencode.ai/zen/v1'),
    ('actual', 'exact-local-model', 'http://127.0.0.1:8080/v1'),
])
def test_established_keyless_policy_still_works(monkeypatch, provider, model, base):
    requests = []
    def respond(req):
        requests.append(req)
        return response_for('openai-api' if provider == 'actual' else 'openai')
    config = route_fixture(monkeypatch, 'openai', respond)
    monkeypatch.setattr(aux, '_scoped_key_env', lambda name: None)
    monkeypatch.setattr('clover_cli.config.get_env_value_prefer_dotenv', lambda name: None)
    config['auxiliary']['presence'] = {'provider': provider, 'model': model, 'base_url':base}
    assert run(config).text == 'synthetic'
    assert len(requests) == 1
    if provider == 'opencode-free': assert not requests[0].headers.get('authorization')


def test_existing_raw_openai_mapping_can_reuse_exact_authenticated_runtime(monkeypatch):
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    monkeypatch.setattr(aux, '_scoped_key_env', lambda name: None)
    with aux.scoped_runtime_main({'provider':'custom', 'requested_provider':'openai',
            'model':config['auxiliary']['presence']['model'],
            'base_url':'https://api.openai.com/v1', 'api_key':'synthetic-runtime-owned'}):
        assert run(config).text == 'synthetic'
    assert len(requests) == 1 and requests[0].headers['authorization'] == 'Bearer synthetic-runtime-owned'


def test_runtime_pool_binding_uses_real_setter_without_rotation(monkeypatch):
    config, pool, requests = genuine_pool_route(monkeypatch, [entry('a'), entry('b')])
    pool._current_id = 'a'
    token = aux.set_runtime_main('openai-codex', config['auxiliary']['presence']['model'],
        base_url=aux._CODEX_AUX_BASE_URL, api_key=pool.current().runtime_api_key, credential_pool=pool)
    try:
        assert run(config).text == 'synthetic'
        assert pool.current().id == 'a' and requests[0][2] == 'a'
    finally: aux.reset_runtime_main(token)


def test_postflight_sync_time_cannot_escape_original_deadline(monkeypatch):
    from agent.bounded_auth import Authority
    requests = []
    def respond(req):
        requests.append(req)
        return response_for('openai')
    config = route_fixture(monkeypatch, 'openai', respond)
    original = Authority.validate
    def slow_postflight(self):
        if requests: time.sleep(.15)
        original(self)
    monkeypatch.setattr(Authority, 'validate', slow_postflight)
    with pytest.raises(TimeoutError): run(config, timeout=.1)
    assert len(requests) == 1


@pytest.mark.parametrize('model,wire', [('exact-free-model','chat'), ('gpt-5-test','responses'), ('claude-sonnet-test','anthropic')])
def test_established_public_keyless_native_wires_never_send_placeholder(monkeypatch, model, wire):
    from clover_cli.models import opencode_zen_free_runtime
    requests = []
    response_provider = {'chat':'openai', 'responses':'openai-api', 'anthropic':'anthropic'}[wire]
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for(response_provider))
    monkeypatch.setattr(aux, '_scoped_key_env', lambda name: None)
    monkeypatch.setattr('clover_cli.config.get_env_value_prefer_dotenv', lambda name: None)
    policy = opencode_zen_free_runtime('opencode-free', model)
    config['auxiliary']['presence'] = {'provider':'opencode-free', 'model':model, 'base_url':policy['base_url']}
    assert run(config).text == 'synthetic'
    assert len(requests) == 1
    assert not requests[0].headers.get('authorization')
    assert not requests[0].headers.get('x-api-key')


def test_missing_explicit_environment_override_does_not_use_ambient(monkeypatch):
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    config['auxiliary']['presence']['key_env'] = 'MISSING_OWNED_KEY'
    with pytest.raises(CompletionHeld, match='credential_override_unavailable'): run(config)
    assert not requests


def test_metadata_import_and_lookup_remain_auth_and_inference_free(monkeypatch):
    import os, subprocess, sys
    script = "import sys; from agent.bounded_provider_metadata import metadata; metadata({}, 'openai-api'); assert 'clover_cli.auth' not in sys.modules; assert 'agent.auxiliary_client' not in sys.modules"
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[2],
                            env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_concurrent_config_change_cannot_pair_new_secret_with_old_endpoint(monkeypatch):
    from agent import bounded_auth
    requests = []
    config = route_fixture(monkeypatch, 'openai', lambda req: requests.append(req) or response_for('openai'))
    config['auxiliary']['presence'] = {'provider':'custom','model':'chosen',
        'base_url':'https://old-owner.invalid/v1', 'api_key':'synthetic-old-owner'}
    original = bounded_auth.metadata
    def switch(source, name):
        config['auxiliary']['presence'].update(base_url='https://new-owner.invalid/v1', api_key='synthetic-new-owner')
        return original(source, name)
    monkeypatch.setattr(bounded_auth, 'metadata', switch)
    with pytest.raises(CompletionHeld, match='configured_route_changed'): run(config)
    assert not requests


def test_select_route_cold_is_supplied_config_only_without_env_catalog_auth_or_writes():
    import os, subprocess, sys
    script = r"""
import builtins, os, sys
from agent.bounded_completion import select_route
original_import = builtins.__import__
forbidden = ('agent.auxiliary_client', 'agent.bounded_auth', 'clover_cli.auth', 'agent.models_dev',
             'clover_cli.runtime_provider', 'run_agent', 'providers')
def guarded_import(name, *a, **kw):
    assert not any(name == prefix or name.startswith(prefix + '.') for prefix in forbidden), name
    return original_import(name, *a, **kw)
builtins.__import__ = guarded_import
os.getenv = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('select_route read environment'))
def audit(event, args):
    if event == 'open':
        path, mode, flags = args
        if isinstance(path, (str, bytes)):
            text = os.fsdecode(path)
            assert not text.endswith(('.json', '.yaml', '.yml', '.env')), text
        assert not (isinstance(mode, str) and any(c in mode for c in 'wax+')), args
        assert not (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)), args
sys.dont_write_bytecode = True
sys.addaudithook(audit)
configs = [
    {'model': {'provider':'openai-api', 'default':'exact-model'}, 'presence': {'enabled':False}},
    {'model': {'provider':'custom','default':'exact-model','base_url':'http://localhost:8080/v1','api_mode':'codex_responses'}, 'auxiliary': {'presence': {'provider':'custom'}}},
    {'model': {'provider':'beans','default':'exact-model'}, 'custom_providers':[{'name':'beans','base_url':'http://localhost:8080/v1'}]},
    {'model': {'provider':'codex','default':'exact-model'}, 'providers': {'codex': {'base_url':'http://localhost:8080/v1'}}},
]
for config in configs:
    route = select_route(config)
    assert route.model == 'exact-model'
assert not any(name in sys.modules for name in forbidden)
"""
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[2],
                            env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

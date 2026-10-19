"""Offline differential request proof; never contacts any provider or real profile."""
import asyncio
import json
import os
import socket

import httpx
import openai
import pytest

from agent import auxiliary_client as aux
from agent.bounded_completion import CompletionHeld, complete
from agent.presence_assessment import messages
from clover_cli.readonly_files import non_mutating_config_reads
from clover_cli.runtime_provider import resolve_runtime_provider
from tests.clover_cli.test_runtime_provider_readonly import profile, inventory
from tests.agent.test_presence_provider_wire import response_for

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Existing POSIX owner proof')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline contract test attempted network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def selected(monkeypatch, tmp_path, respond):
    home, config, credential = profile(monkeypatch, tmp_path)
    runtime = resolve_runtime_provider(read_only=True)
    # Only the transport edge is scripted; SDK/TLS/config/client factories stay real.
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda self, req: respond(req))
    async def async_respond(self, req):
        return respond(req)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', async_respond)
    config['presence'] = {'budget_mode': 'requests'}
    return home, config, credential, runtime


def invoke(config, runtime, text='Synthetic current-turn evidence.'):
    with non_mutating_config_reads(), aux.scoped_runtime_main(runtime):
        return asyncio.run(complete(config=config, messages=messages(text), max_output_tokens=64, timeout=3))


def test_bounded_payload_matches_normal_main_and_auxiliary_sdk_core(monkeypatch, tmp_path):
    wires = []
    def respond(req):
        wires.append(req)
        return response_for('openai-codex')
    home, config, credential, runtime = selected(monkeypatch, tmp_path, respond)
    text = 'Synthetic current-turn evidence.'
    before = inventory(home)
    assert invoke(config, runtime, text).text == 'synthetic'
    assert inventory(home) == before
    bounded = json.loads(wires[-1].content)
    from agent.transports.codex import ResponsesApiTransport
    from agent.codex_responses_adapter import _preflight_codex_api_kwargs
    main = ResponsesApiTransport().build_kwargs(runtime['model'], messages(text),
        provider='openai-codex', base_url=runtime['base_url'], is_codex_backend=True)
    assert _preflight_codex_api_kwargs(bounded, allow_stream=True) == bounded
    for key in ('model', 'instructions', 'input', 'store'):
        assert main[key] == bounded[key]
    assert set(bounded) == {'model', 'instructions', 'input', 'store', 'stream'}
    assert bounded['model'] == 'gpt-6-astra' and bounded['stream'] is True
    assert not {'temperature', 'max_output_tokens', 'extra_headers', 'prompt_cache_retention'} & set(bounded)
    # Exercise the ordinary auxiliary adapter through the actual OpenAI SDK serializer.
    with non_mutating_config_reads(), aux.scoped_runtime_main(runtime):
        client = openai.OpenAI(api_key=credential.access_token, base_url=runtime['base_url'],
            default_headers=aux._codex_cloudflare_headers(credential.access_token, base_url=runtime['base_url']),
            **aux._openai_http_client_kwargs(runtime['base_url']))
        try:
            aux._CodexCompletionsAdapter(client, runtime['model']).create(
                model=runtime['model'], messages=messages(text), timeout=3)
        finally:
            client.close()
    normal = json.loads(wires[-1].content)
    assert {k: normal[k] for k in bounded} == bounded
    assert set(normal) - set(bounded) == {'prompt_cache_key'}
    assert wires[0].url == wires[1].url
    for key in ('authorization', 'chatgpt-account-id'):
        assert wires[0].headers[key] == wires[1].headers[key]
    assert len(wires) == 2


@pytest.mark.parametrize('mutation', ['profile', 'source', 'owner', 'permissions', 'endpoint'])
def test_selected_authority_revocation_before_physical_dispatch_holds(monkeypatch, tmp_path, mutation):
    wires = []
    home, config, _, runtime = selected(monkeypatch, tmp_path, lambda req: wires.append(req))
    factory = aux._openai_http_client_kwargs
    changed = False
    def mutate(*args, **kwargs):
        nonlocal changed
        result = factory(*args, **kwargs)
        if kwargs.get('async_mode') and not changed:
            changed = True
            if mutation == 'profile':
                monkeypatch.setenv('CLOVER_HOME', str(tmp_path/'different-profile'))
            elif mutation == 'permissions':
                (home/'auth.json').chmod(0o644)
            elif mutation == 'endpoint':
                aux._RUNTIME_MAIN_CONTEXT.get()['base_url'] = 'https://example.invalid/changed'
            else:
                auth = home/'auth.json'
                store = json.loads(auth.read_text())
                if mutation == 'source': store['authority_version'] = 'revoked'
                else: store['credential_pool']['openai-codex'][0]['last_status'] = 'dead'
                auth.write_text(json.dumps(store))
        return result
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', mutate)
    with pytest.raises((CompletionHeld, ValueError)):
        invoke(config, runtime)
    assert changed and wires == []


def test_http400_retains_only_bounded_structured_diagnostics(monkeypatch, tmp_path):
    wires = []
    def respond(req):
        wires.append(req)
        return httpx.Response(400, headers={'Authorization': 'Bearer SCRIPTED-secret',
            'set-cookie': 'session=SCRIPTED-secret', 'x-request-id': 'PRIVATE-user-id'},
            json={'error': {'type': 'invalid_request_error', 'code': 'unsupported_parameter',
                'param': 'max_output_tokens',
                'message': 'PRIVATE prompt; person@example.invalid; Bearer SCRIPTED-secret'}})
    _, config, _, runtime = selected(monkeypatch, tmp_path, respond)
    with pytest.raises(CompletionHeld) as raised:
        invoke(config, runtime)
    diagnostic = getattr(raised.value, 'diagnostic', None)
    assert isinstance(diagnostic, dict), 'HTTP failure discards existing structured error body'
    assert diagnostic == {'http_status': 400, 'body_status': 'json',
        'error_type': 'invalid_request_error', 'error_code': 'unsupported_parameter',
        'error_param': 'max_output_tokens', 'usage': 'unknown', 'cost': None}
    serialized = json.dumps(diagnostic) + str(raised.value)
    assert all(secret not in serialized for secret in ('SCRIPTED-secret', 'PRIVATE', 'person@'))
    assert raised.value.reason == 'provider_http_400_usage_unknown'
    assert len(wires) == 1


@pytest.mark.parametrize('body, expected', [
    (b'', 'empty'), (b'<html>PRIVATE-untrusted-error</html>', 'invalid_json'),
    (b'{"error":', 'invalid_json'), (b'\xffPRIVATE', 'invalid_json'),
    (b'x' * 16385, 'truncated'),
    (b'{"error":{"type":{"secret":"PRIVATE"},"code":["PRIVATE"],"param":false}}', 'json'),
    (b'{"error":{"type":"PRIVATE-secret","code":"person@example.invalid","param":"PRIVATE-input"}}', 'json'),
    (b'{"error":"PRIVATE-prompt"}', 'json'), (b'[]', 'json'),
])
def test_http400_untrusted_body_cannot_escape_safe_vocabulary(monkeypatch, tmp_path, body, expected):
    wires = []
    def respond(req):
        wires.append(req)
        return httpx.Response(400, content=body, headers={'set-cookie': 'PRIVATE-secret'})
    _, config, _, runtime = selected(monkeypatch, tmp_path, respond)
    with pytest.raises(CompletionHeld) as raised:
        invoke(config, runtime)
    diagnostic = raised.value.diagnostic
    assert isinstance(diagnostic, dict)
    assert diagnostic['body_status'] == expected
    assert diagnostic['error_code'] is diagnostic['error_type'] is diagnostic['error_param'] is None
    assert diagnostic['cost'] is None and diagnostic['usage'] == 'unknown'
    assert 'PRIVATE' not in json.dumps(diagnostic) and len(wires) == 1


@pytest.mark.parametrize('failure', ['large', 'stall', 'broken'])
def test_http400_error_stream_has_byte_time_and_close_bounds(monkeypatch, tmp_path, failure):
    chunks = []
    closed = []
    wires = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if failure == 'stall':
                await asyncio.Event().wait()
            if failure == 'broken':
                raise httpx.ReadError('PRIVATE-secret-bearing-url')
            for i in range(1000):
                chunks.append(i)
                yield b'x' * 100
        async def aclose(self):
            closed.append(True)
    def respond(req):
        wires.append(req)
        return httpx.Response(400, stream=Body())
    _, config, _, runtime = selected(monkeypatch, tmp_path, respond)
    with pytest.raises(CompletionHeld) as raised:
        invoke(config, runtime)
    expected = {'large': 'truncated', 'stall': 'timed_out', 'broken': 'read_error'}[failure]
    assert raised.value.reason == 'provider_http_400_usage_unknown'
    assert isinstance(raised.value.diagnostic, dict)
    assert raised.value.diagnostic['body_status'] == expected
    assert closed == [True] and len(wires) == 1 and len(chunks) <= 164
    assert 'PRIVATE' not in json.dumps(raised.value.diagnostic)


def test_isolated_evaluator_preserves_safe_http400_and_charges_once(monkeypatch, tmp_path):
    from tests.test_presence_quality_evaluator import evaluator
    wires = []
    def respond(req):
        wires.append(req)
        return httpx.Response(400, json={'error': {'type': 'invalid_request_error',
            'code': 'invalid_value', 'param': 'model', 'message': 'PRIVATE-prompt'},
            'usage': {'output_tokens': 0}, 'cost': 0})
    home, _, _, _ = selected(monkeypatch, tmp_path, respond)
    before = inventory(home)
    quota = tmp_path/'quota'
    quota.mkdir(mode=0o700)
    module = evaluator()
    cases, _ = module.load_cases('normative')
    results, attempts, invocations = module.execute_isolated_cases(cases[:2], max_calls=3,
        quota_dir=quota, request_budget_consent=True)
    assert attempts == invocations == len(wires) == 1
    assert json.loads((quota/'presence-eval-quota.json').read_text())['used'] == 1
    assert results[0]['reason'] == 'provider_http_400_usage_unknown'
    assert results[0]['provider_diagnostic']['error_param'] == 'model'
    assert results[0]['provider_diagnostic']['usage'] == 'unknown'
    assert results[0]['provider_diagnostic']['cost'] is None
    assert results[0]['output_usage']['output_tokens'] is None
    assert results[1]['reason'] == 'stopped_after_error_no_retry'
    assert results[1]['reserved'] is False and results[1]['physical_requests'] == 0
    assert inventory(home) == before and 'PRIVATE' not in json.dumps(results)


@pytest.mark.parametrize('revocation', ['config', 'managed', 'stop', 'live_store'])
def test_isolated_evaluator_rechecks_controls_at_physical_dispatch(monkeypatch, tmp_path, revocation):
    from tests.test_presence_quality_evaluator import evaluator
    wires = []
    home, _, _, _ = selected(monkeypatch, tmp_path,
        lambda req: (wires.append(req), response_for('openai-codex'))[1])
    quota = tmp_path/'quota'
    quota.mkdir(mode=0o700)
    factory = aux._openai_http_client_kwargs
    changed = False
    def mutate(*args, **kwargs):
        nonlocal changed
        result = factory(*args, **kwargs)
        if kwargs.get('async_mode') and not changed:
            changed = True
            if revocation == 'config':
                import yaml
                path = home/'config.yaml'
                config = yaml.safe_load(path.read_text())
                config['model']['default'] = 'revoked-configured-model'
                path.write_text(yaml.safe_dump(config))
            elif revocation == 'managed':
                (tmp_path/'managed'/'config.yaml').write_text('presence:\n  enabled: false\n')
            elif revocation == 'stop':
                (quota/'STOP').touch()
            else:
                (home/'cron').mkdir(mode=0o700)
                (home/'cron'/'presence.db').write_bytes(b'PRIVATE-do-not-open')
        return result
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', mutate)
    module = evaluator()
    cases, _ = module.load_cases('normative')
    results, attempts, invocations = module.execute_isolated_cases(cases[:1], max_calls=1,
        quota_dir=quota, request_budget_consent=True)
    assert changed
    assert wires == [], 'revoked experiment dispatched a physical provider request'
    assert results[0]['status'] == 'not_measured'
    # Reservation remains charged even though the final boundary refused dispatch.
    assert attempts == invocations == 1
    assert json.loads((quota/'presence-eval-quota.json').read_text())['used'] == 1


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'gemini', 'openai-codex', 'openai-api'])
def test_http400_across_supported_wires_never_retries_or_claims_zero_cost(monkeypatch, provider):
    from tests.agent.test_presence_provider_wire import route_fixture
    wires = []
    def respond(req):
        wires.append(req)
        return httpx.Response(400, json={'error': {'type': 'invalid_request_error',
            'code': 'invalid_value', 'param': 'model', 'message': 'PRIVATE-evidence'},
            'usage': {'output_tokens': 0}, 'cost': 0})
    config = route_fixture(monkeypatch, provider, respond)
    with pytest.raises((CompletionHeld, openai.APIStatusError)) as raised:
        asyncio.run(complete(config=config, messages=messages('Synthetic input'),
            max_output_tokens=64, timeout=2))
    assert len(wires) == 1
    if isinstance(raised.value, CompletionHeld):
        assert isinstance(raised.value.diagnostic, dict)
        assert raised.value.diagnostic['http_status'] == 400
        assert raised.value.diagnostic['usage'] == 'unknown'
        assert raised.value.diagnostic['cost'] is None
        assert 'PRIVATE' not in json.dumps(raised.value.diagnostic)
    else:
        # The ordinary SDK already retains its error body; we do not log it.
        assert raised.value.status_code == 400


def test_cancellation_during_error_diagnostics_closes_without_retry(monkeypatch, tmp_path):
    started = asyncio.Event()
    closed = []
    wires = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b'PRIVATE-never-completed'
        async def aclose(self):
            closed.append(True)
    def respond(req):
        wires.append(req)
        return httpx.Response(400, stream=Body())
    _, config, _, runtime = selected(monkeypatch, tmp_path, respond)
    async def run():
        with non_mutating_config_reads(), aux.scoped_runtime_main(runtime):
            task = asyncio.create_task(complete(config=config, messages=messages('Synthetic input'),
                max_output_tokens=64, timeout=3))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
    assert wires and len(wires) == 1 and closed == [True]

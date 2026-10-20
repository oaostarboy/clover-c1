"""Joined paths. All model text is SCRIPTED; no emotional-quality claim.

Source transcripts are byte-identical to the emotional-behavior fixtures.
The configured-route fixture derives from the independent six-route probe.
"""
import json
import sqlite3

import httpx
import pytest
import yaml

from agent import auxiliary_client as aux
from agent.credential_pool import PooledCredential, load_pool
from agent.presence_state import ContextKey
from cron.presence import ThoughtStore
from tests.agent.test_provider_security_corrected import jwt
from tests.gateway.test_emotions_command import inbound_command
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.gateway.test_workstream_suggestion_offer import scope

REAL_RESOLVER = aux.resolve_provider_client
TEXT = 'I wonder how deployment packaging could be safer.'
LATER = 'Deployment packaging now has a manifest checksum before publishing.'


@pytest.mark.parametrize('route', ['selected', 'openai', 'anthropic', 'gemini',
                                   'openai-api', 'openai-codex', 'custom'])
def test_normal_current_interest_to_exact_route_capture_status_and_one_later_ack(
        monkeypatch, tmp_path, record_property, route):
    h, stub_requests, bot = setup_host(monkeypatch, tmp_path)
    provider = 'openai' if route == 'selected' else route
    observed, clients, contexts = [], [], []
    pool = owner = None
    token = jwt('exact-explicit-account') if provider == 'openai-codex' else 'synthetic-task-key'
    model = {'anthropic': 'claude-haiku-4-5', 'gemini': 'gemini-2.5-flash',
             'openai-codex': 'gpt-6-astra-900k'}.get(provider, 'literal-selected-model')
    mode = 'strict_tokens' if provider in {'openai', 'anthropic', 'openai-api'} else 'requests'
    task = {'provider': provider, 'model': model}
    if provider == 'custom':
        task['base_url'] = 'http://127.0.0.1:8866/v1'
    else:
        task['api_key'] = token
    cfg = yaml.safe_load((h.home / 'config.yaml').read_text())
    cfg['presence']['budget_mode'] = mode
    cfg['proactive']['aux'] = {'max_calls_per_event': 1, 'max_calls_per_day': 3, 'max_output_tokens': 512}
    if route == 'selected':
        model = 'gpt-4o-mini'
        selected = PooledCredential(provider='openai', id='exact-selected', label='synthetic selected',
            source='manual', auth_type='api_key', access_token=token, priority=0)
        sibling = PooledCredential(provider='openai', id='unselected', label='synthetic unselected',
            source='manual', auth_type='api_key', access_token='synthetic-sibling-key', priority=1)
        auth = h.home / 'auth.json'
        auth.write_text(json.dumps({'version': 1, 'authority_version': 7,
            'credential_pool': {'openai': [selected.to_dict(), sibling.to_dict()]}}))
        pool = load_pool('openai')
        assert pool.select().id == 'exact-selected'
        owner = pool.current()
        cfg['model'] = {'provider': 'openai', 'default': model, 'base_url': 'https://api.openai.com/v1'}
        task = {'provider': 'auto', 'model': 'auto'}
        monkeypatch.setattr(h.runner, '_resolve_session_agent_runtime', lambda **kw: (model, dict(
            provider='openai', api_key=token, base_url='https://api.openai.com/v1',
            api_mode='chat_completions', credential_pool=pool)))
        def forbidden(*a, **kw):
            pytest.fail('bounded work selected/refreshed another account')
        monkeypatch.setattr(pool, 'select', forbidden)
        monkeypatch.setattr(pool, '_refresh_entry', forbidden)
    cfg['auxiliary']['presence'] = task
    (h.home / 'config.yaml').write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr('agent.image_routing._lookup_supports_vision', lambda *a, **kw: False)
    config_before = (h.home / 'config.yaml').read_bytes()
    auth_before = (h.home / 'auth.json').read_bytes() if (h.home / 'auth.json').exists() else None
    monkeypatch.setattr(aux, 'resolve_provider_client', REAL_RESOLVER)
    real_completion = aux.presence_completion
    async def completion(**kwargs):
        runtime = aux._RUNTIME_MAIN_CONTEXT.get() or {}
        contexts.append({'provider': runtime.get('provider'),
                         'exact_pool': pool is not None and runtime.get('credential_pool') is pool})
        return await real_completion(**kwargs)
    monkeypatch.setattr(aux, 'presence_completion', completion)
    def respond(request):
        payload = json.loads(request.content)
        current = 'valence' in json.dumps(payload)
        with sqlite3.connect(ThoughtStore().path) as conn:
            reserved = conn.execute('SELECT COUNT(*) FROM presence_budget').fetchone()[0]
        assert reserved == len(observed) + 1
        assert reserved <= 3
        assert request.url.host == {'openai': 'api.openai.com', 'anthropic': 'api.anthropic.com',
            'gemini': 'generativelanguage.googleapis.com', 'openai-api': 'api.openai.com',
            'openai-codex': 'chatgpt.com', 'custom': '127.0.0.1'}[provider]
        if pool is not None:
            assert pool.current() is owner
        if provider == 'custom':
            assert request.headers.get('authorization') == 'Bearer no-key-required'
        elif provider == 'openai-codex':
            assert request.headers['chatgpt-account-id'] == 'exact-explicit-account'
            assert payload['model'] == 'gpt-6-astra' and 'max_output_tokens' not in payload
        elif provider == 'anthropic':
            assert request.headers['x-api-key'] == token and payload['max_tokens'] == 512
        elif provider == 'gemini':
            assert request.headers['x-goog-api-key'] == token
        else:
            assert request.headers['authorization'] == 'Bearer ' + token
        observed.append({'url': str(request.url), 'payload': payload, 'reserved': reserved,
                         'phase': 'assessment' if current else 'initiative'})
        body = ({'affect': {'valence': -.6, 'arousal': .2, 'confidence': .9, 'posture': 'gentle'},
                 'thought': {'summary': TEXT, 'quote': TEXT, 'confidence': .9,
                             'owner': 'current_human', 'intent': 'unresolved_question', 'source_context': 'direct'}} if current else {
                    'connection': 'Deployment packaging can verify manifest checksum before publishing.',
                    'evidence_indices': [0, 1], 'confidence': .9})
        text = json.dumps(body)
        if provider == 'anthropic':
            return httpx.Response(200, json={'content': [{'type': 'text', 'text': text}],
                                           'usage': {'output_tokens': 3}})
        if provider == 'gemini':
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': text}]}}],
                'usageMetadata': {'candidatesTokenCount': 3, 'thoughtsTokenCount': 0}})
        if provider in {'openai-api', 'openai-codex'}:
            event = {'type': 'response.completed', 'response': {'output': [{'type': 'message',
                'content': [{'type': 'output_text', 'text': text}]}], 'usage': {'output_tokens': 3}}}
            return httpx.Response(200, content='data: ' + json.dumps(event) + '\n\n')
        return httpx.Response(200, json={'choices': [{'message': {'content': text}}],
                                       'usage': {'completion_tokens': 3}})
    def factory(*args, **kw):
        client = (httpx.AsyncClient if kw.get('async_mode') else httpx.Client)(
            transport=httpx.MockTransport(respond))
        clients.append(client)
        return {'http_client': client}
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', factory)
    try:
        h.inbound_human(TEXT, message_id=100)
        key = ContextKey(scope(h), h.session_id())
        pending = ThoughtStore().list(key)
        assert len(observed) == 1 and not stub_requests
        assert len(pending) == 1 and pending[0].summary == TEXT
        assert json.loads(pending[0].source.source_id) == [h.session_id(), h.user_rows()[0]['id']]
        wire = h.scripted.requests[-1]
        assert 'Tentative interaction posture: gentle' in wire['messages'][-1]['content']
        assert h.user_rows()[0]['content'] == TEXT
        assert bot.send_message.await_count == 0 and not ThoughtStore().publications()
        before = len(observed), ThoughtStore().generation()
        # Ordinary main startup may canonicalize its temporary configuration.
        # The read-only status command itself must preserve the exact bytes.
        config_before = (h.home / 'config.yaml').read_bytes()
        status = inbound_command(h, '/emotions')
        assert (h.home / 'config.yaml').read_bytes() == config_before
        assert 'confidence=0.90' in status and 'gentle' in status
        assert before == (len(observed), ThoughtStore().generation())
        h.inbound_human(LATER, message_id=103)
        assert len(observed) == 2 and observed[-1]['phase'] == 'initiative'
        assert bot.send_message.await_count == 1
        assert [p['state'] for p in ThoughtStore().publications()] == ['acknowledged']
        later = h.scripted.requests[-1]
        assert later['messages'][:len(wire['messages'])] == wire['messages']
        assert later.get('tools') == wire.get('tools')
        assert [r['role'] for r in h.db.get_messages(h.session_id()) if r['role'] != 'session_meta'] == ['user', 'assistant'] * 2
        assert all(c.is_closed for c in clients)
        assert (h.home / 'config.yaml').read_bytes() == config_before
        assert ((h.home / 'auth.json').read_bytes() if (h.home / 'auth.json').exists() else None) == auth_before
        if route == 'selected':
            assert pool.current() is owner
            assert len(contexts) == 2 and all(c['provider'] == 'openai' and c['exact_pool'] for c in contexts)
        record_property('SCRIPTED_route_wire', json.dumps(observed))
        record_property('selected_runtime_contexts', json.dumps(contexts))
        record_property('emotions_status', status)
    finally:
        h.close()

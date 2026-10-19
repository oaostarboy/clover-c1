"""Real application; all assessment outputs are SCRIPTED, not model accuracy."""
import json
import sqlite3

import httpx
import yaml

from agent import auxiliary_client as aux
from cron.presence import ThoughtStore
from tests.gateway.test_presence_integration import setup_host, native_command
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401

REAL_RESOLVER = aux.resolve_provider_client


def scripted_host(monkeypatch, tmp_path, outputs):
    h, unused, bot = setup_host(monkeypatch, tmp_path)
    config = yaml.safe_load((h.home / 'config.yaml').read_text(encoding='utf-8'))
    config['presence']['budget_mode'] = 'requests'
    config['auxiliary']['presence'] = {
        'provider': 'custom', 'model': 'SCRIPTED-current-turn',
        'base_url': 'https://scripted-current.invalid/v1', 'api_key': 'synthetic-task-key'}
    (h.home / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
    monkeypatch.setattr(aux, 'resolve_provider_client', REAL_RESOLVER)
    requests = []

    async def respond(request):
        with sqlite3.connect(ThoughtStore().path) as conn:
            reserved = conn.execute('SELECT COUNT(*) FROM presence_budget').fetchone()[0]
        assert reserved == len(requests) + 1
        assert str(request.url) == 'https://scripted-current.invalid/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer synthetic-task-key'
        payload = json.loads(request.content)
        assert payload['model'] == 'SCRIPTED-current-turn'
        assert payload['max_tokens'] <= 512
        requests.append(payload)
        sample = outputs[len(requests) - 1]
        if callable(sample):
            sample = sample(h)
        return httpx.Response(200, json={
            'id': 'SCRIPTED', 'object': 'chat.completion', 'created': 0,
            'model': 'SCRIPTED-current-turn', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps(sample)}}]})

    def kwargs(*args, **kw):
        assert kw.get('async_mode')
        return {'http_client': httpx.AsyncClient(transport=httpx.MockTransport(respond))}

    monkeypatch.setattr(aux, '_openai_http_client_kwargs', kwargs)
    return h, requests, bot


def assessment(posture='gentle', valence=-.6, arousal=.2, confidence=.9, thought=None):
    return {'affect': {'valence': valence, 'arousal': arousal, 'confidence': confidence,
                       'posture': posture}, 'thought': thought}


def test_candidate_assessment_origins(record_property):
    import importlib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    names = ['agent.presence_assessment', 'agent.presence_runtime', 'agent.presence_state',
             'agent.presence_initiation', 'agent.emotions', 'agent.bounded_completion']
    origins = {name: str(Path(importlib.import_module(name).__file__).resolve()) for name in names}
    assert all(Path(p).is_relative_to(root) for p in origins.values())
    record_property('candidate_assessment_origins', json.dumps(origins, sort_keys=True))


def test_non_keyword_french_sadness_reaches_real_compose_and_status(monkeypatch, tmp_path):
    h, requests, bot = scripted_host(monkeypatch, tmp_path, [assessment()])
    text = "Depuis son départ, tout me semble vide. Peux-tu rester simple ?"
    try:
        h.inbound_human(text, message_id=100)
        assert len(requests) == 1, 'ordinary French current turn was not assessed'
        wire = h.scripted.requests[-1]['messages']
        assert 'Tentative interaction posture: gentle' in wire[-1]['content']
        assert h.user_rows()[-1]['content'] == text
        status = native_command(h, '/emotions', 101)
        assert 'gentle' in status and 'confidence=0.90' in status
        assert len(requests) == 1 and bot.send_message.await_count == 0
        assert not ThoughtStore().publications()
    finally:
        h.close()


def test_ordinary_question_is_captured_without_manual_command_then_later_offered(monkeypatch, tmp_path):
    from agent.presence_state import ContextKey
    from tests.gateway.test_workstream_suggestion_offer import scope
    text = 'I wonder how deployment packaging could be safer.'
    thought = {'summary': text, 'quote': text, 'confidence': .9, 'owner': 'current_human', 'intent': 'unresolved_question', 'source_context': 'direct'}
    connection = {'connection': 'Deployment packaging can verify manifest checksum before publishing.',
                  'evidence_indices': [0, 1], 'confidence': .9}
    h, requests, bot = scripted_host(monkeypatch, tmp_path,
        [assessment('ordinary', 0, .2, thought=thought), connection])
    try:
        h.inbound_human(text, message_id=100)
        key = ContextKey(scope(h), h.session_id())
        pending = ThoughtStore().list(key)
        assert len(pending) == 1, 'ordinary unresolved interest was not captured'
        assert pending[0].summary == text
        assert json.loads(pending[0].source.source_id) == [h.session_id(), h.user_rows()[0]['id']]
        assert bot.send_message.await_count == 0 and not ThoughtStore().publications()
        h.inbound_human('Deployment packaging now has a manifest checksum before publishing.', message_id=101)
        assert len(requests) == 2, 'later event did not use one connection assessment'
        assert 'manifest checksum' in requests[1]['messages'][1]['content']
        assert bot.send_message.await_count == 1
        assert [p['state'] for p in ThoughtStore().publications()] == ['acknowledged']
        wire = h.scripted.requests[-1]['messages']
        assert [m['role'] for m in wire] == ['system', 'user', 'assistant', 'user']
        assert [m['content'] for m in wire if m['role'] == 'assistant'] == ['ack']
    finally:
        h.close()


def test_rapid_ordinary_turns_do_not_spend_another_assessment(monkeypatch, tmp_path):
    h, requests, _ = scripted_host(monkeypatch, tmp_path, [assessment(), assessment()])
    try:
        h.inbound_human('I feel down today.', message_id=100)
        h.inbound_human('It still feels heavy.', message_id=101)
        assert len(requests) == 1, 'minimum interval did not hold current assessment'
    finally:
        h.close()

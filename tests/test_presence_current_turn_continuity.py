"""Admission/novelty and evaluation utility contracts, synthetic wire only."""
import json
from datetime import timedelta
from pathlib import Path
import importlib.util

from agent.presence_state import ContextKey
from cron.presence import ThoughtStore
from tests.test_presence_current_turn_behavior import scripted_host, assessment
from tests.gateway.test_presence_integration import native_command
from tests.gateway.test_workstream_suggestion_offer import scope
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def test_automatic_thought_cannot_offer_connection_that_ignores_new_context(monkeypatch, tmp_path):
    quote = 'I wonder how deployment packaging could be safer.'
    source = quote + ' The old manifest checksum matches.'
    old_connection = {'connection': 'Deployment packaging can verify manifest checksum before publishing.',
                      'evidence_indices': [0], 'confidence': .9}
    h, requests, bot = scripted_host(monkeypatch, tmp_path,
        [assessment(thought={'summary': source, 'quote': source, 'confidence': .9, 'owner': 'current_human', 'intent': 'unresolved_question', 'source_context': 'direct'}), old_connection])
    try:
        h.inbound_human(source, message_id=100)
        h.inbound_human('Deployment packaging now has compression benchmarks and reproducible archives.', message_id=101)
        assert len(requests) == 2
        assert not ThoughtStore().publications() and bot.send_message.await_count == 0
    finally:
        h.close()


def test_repeated_source_and_irrelevant_new_context_make_no_initiative_call(monkeypatch, tmp_path):
    from agent import presence_runtime as runtime
    text = 'I wonder how deployment packaging could be safer.'
    h, requests, bot = scripted_host(monkeypatch, tmp_path,
        [assessment(thought={'summary': text, 'quote': text, 'confidence': .9, 'owner': 'current_human', 'intent': 'unresolved_question', 'source_context': 'direct'}),
         {'affect': None, 'thought': None}])
    now = [runtime.clock()]
    monkeypatch.setattr(runtime, 'clock', lambda: now[0])
    try:
        h.inbound_human(text, message_id=100)
        now[0] += timedelta(seconds=7)
        h.inbound_human(text, message_id=101)
        assert len(requests) == 1 and bot.send_message.await_count == 0
        now[0] += timedelta(seconds=7)
        h.inbound_human('Let us talk about sourdough bread recipes.', message_id=102)
        assert len(requests) == 2  # New ordinary affect/extraction, not an initiative call.
        assert all('current human' in r['messages'][0]['content'] for r in requests)
        assert len(ThoughtStore().list(ContextKey(scope(h), h.session_id()))) == 1
        assert not ThoughtStore().publications()
    finally:
        h.close()


def test_gentle_reading_expires_without_status_spending_or_emotional_history(monkeypatch, tmp_path):
    from agent import presence_runtime as runtime, emotions
    h, requests, _ = scripted_host(monkeypatch, tmp_path, [assessment()])
    now = [runtime.clock()]
    monkeypatch.setattr(runtime, 'clock', lambda: now[0])
    monkeypatch.setattr(emotions, 'clock', lambda: now[0])
    try:
        h.inbound_human('The house feels empty now.', message_id=100)
        assert 'gentle' in native_command(h, '/emotions', 101)
        now[0] += timedelta(seconds=121)
        status = native_command(h, '/emotions', 102)
        assert 'unknown (expired)' in status and 'posture: ordinary' in status
        assert len(requests) == 1
        assert not ThoughtStore().list(ContextKey(scope(h), h.session_id()))
    finally:
        h.close()


def test_current_connection_evidence_deleted_at_attempt_cannot_publish(monkeypatch, tmp_path):
    text = 'I wonder how deployment packaging could be safer.'
    connection = {'connection': 'Deployment packaging can verify manifest checksum before publishing.',
                  'evidence_indices': [0, 1], 'confidence': .9}
    h, requests, bot = scripted_host(monkeypatch, tmp_path,
        [assessment(thought={'summary': text, 'quote': text, 'confidence': .9, 'owner': 'current_human', 'intent': 'unresolved_question', 'source_context': 'direct'}), connection])
    original = ThoughtStore.attempt
    def attempt(store, *args):
        result = original(store, *args)
        row_id = h.user_rows()[-1]['id']
        h.db._execute_write(lambda conn: conn.execute('DELETE FROM messages WHERE id=?', (row_id,)))
        return result
    monkeypatch.setattr(ThoughtStore, 'attempt', attempt)
    try:
        h.inbound_human(text, message_id=100)
        h.inbound_human('Deployment packaging now has a manifest checksum before publishing.', message_id=101)
        assert len(requests) == 2
        assert bot.send_message.await_count == 0
        assert all(p['state'] != 'acknowledged' for p in ThoughtStore().publications())
    finally:
        h.close()


def test_manual_thought_also_rechecks_uncited_current_context_at_attempt(monkeypatch, tmp_path):
    connection = {'connection': 'Deployment packaging can verify manifest checksum before publishing.',
                  'evidence_indices': [0], 'confidence': .9}
    h, requests, bot = scripted_host(monkeypatch, tmp_path, [connection])
    original = ThoughtStore.attempt
    def attempt(store, *args):
        result = original(store, *args)
        row_id = h.user_rows()[-1]['id']
        h.db._execute_write(lambda conn: conn.execute('DELETE FROM messages WHERE id=?', (row_id,)))
        return result
    monkeypatch.setattr(ThoughtStore, 'attempt', attempt)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        h.inbound_human('Help verify deployment packaging', message_id=101)
        assert len(requests) == 1
        assert bot.send_message.await_count == 0
        assert all(p['state'] != 'acknowledged' for p in ThoughtStore().publications())
    finally:
        h.close()


def test_desktop_french_ordinary_interest_uses_same_assessment_and_one_later_ack(monkeypatch, tmp_path):
    import time
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    quote = "Je m'intéresse au déploiement logiciel."
    connection = {'connection': 'Le déploiement logiciel peut vérifier les sommes de contrôle et les signatures.',
                  'evidence_indices': [0, 1], 'confidence': .9}
    h, requests, _ = scripted_host(monkeypatch, tmp_path,
        [assessment('ordinary', .2, .2, thought={'summary': quote, 'quote': quote, 'confidence': .9, 'owner': 'current_human', 'intent': 'unresolved_question', 'source_context': 'direct'}), connection])
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    try:
        send(quote)
        assert len(requests) == 1 and not ThoughtStore().publications()
        complete = send('Le déploiement logiciel dispose maintenant de sommes de contrôle et de signatures.')
        result = server.dispatch({'id': 'later-context', 'method': 'presence.after_reply',
            'params': {'session_id': sid, 'token': complete['presence_after_reply_token']}}, transport=transport)
        assert result['result']['accepted']
        deadline = time.monotonic() + 7
        while not ThoughtStore().publications() or ThoughtStore().publications()[0]['state'] != 'acknowledged':
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert len(requests) == 2
        assert len([f for f in transport.frames if f.get('params', {}).get('type') == 'presence.initiative']) == 1
        rows = [r for r in h.db.get_messages('desktop-stored') if r['role'] != 'session_meta']
        assert [r['role'] for r in rows] == ['user', 'assistant', 'user', 'assistant']
    finally:
        agent.close()
        h.close()


def load_evaluator():
    path = Path(__file__).resolve().parents[1] / 'scripts/eval_presence_current_turn.py'
    spec = importlib.util.spec_from_file_location('eval_presence_current_turn', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evaluator_defaults_to_no_provider_calls(capsys):
    assert load_evaluator().main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['mode'] == 'dry_run' and result['provider_calls'] == 0
    assert result['available_cases'] >= 20


def test_evaluator_opt_in_branch_uses_real_configured_scripted_route(monkeypatch, tmp_path, capsys):
    h, requests, _ = scripted_host(monkeypatch, tmp_path, [assessment()])
    try:
        assert load_evaluator().main(['--execute-provider-calls', '--case', 'sadness_fr']) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['attempts_reserved'] == result['valid_schemas'] == 1
        assert result['normative_posture_matches'] == 1
        assert len(requests) == 1
    finally:
        h.close()

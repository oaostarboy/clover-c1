"""SCRIPTED offline probe: normal selected-main entry -> unchanged evaluator.

No product correction. Temporary synthetic profile/auth only; never live auth.
"""
import json
import sqlite3

import httpx
import pytest
import yaml

from agent import auxiliary_client as aux
from clover_cli.runtime_provider import resolve_runtime_provider
from cron.presence import ThoughtStore
from tests.agent.test_provider_security_corrected import entry
from tests.test_presence_quality_evaluator import evaluator, scripted_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


@pytest.mark.parametrize('control', ['selected', 'missing_owner', 'lower_budget', 'stop'])
def test_normal_main_entry_scopes_cli_without_forged_owner(monkeypatch, tmp_path, capsys, record_property, control):
    h, unused, bot = scripted_host(monkeypatch, tmp_path, [])
    path = h.home / 'config.yaml'
    config = yaml.safe_load(path.read_text())
    config['model'] = {'provider': 'openai-codex', 'default': 'gpt-6-astra-900k'}
    config['auxiliary']['presence'] = {'provider': 'auto', 'model': 'auto'}
    config['proactive']['aux'] = {'max_calls_per_event': 1, 'max_calls_per_day': 1, 'max_output_tokens': 64}
    path.write_text(yaml.safe_dump(config))
    credential = entry('SCRIPTED-normal-main')
    auth = h.home / 'auth.json'
    auth.write_text(json.dumps({'version': 1, 'credential_pool': {'openai-codex': [credential.to_dict()]}}))
    original_config, original_auth = path.read_bytes(), auth.read_bytes()
    wires = []

    def respond(request):
        assert request.url.host == 'chatgpt.com'
        assert request.headers['chatgpt-account-id'] == 'SCRIPTED-normal-main'
        assert request.headers['authorization'] == 'Bearer ' + credential.access_token
        payload = json.loads(request.content)
        assert payload['model'] == 'gpt-6-astra' and payload['stream'] is True and payload['store'] is False
        assert 'max_output_tokens' not in payload  # Request mode never claims a server token/cost cap.
        from agent.presence_assessment import PROMPT
        assert payload['instructions'] == PROMPT
        with sqlite3.connect(ThoughtStore().path) as db:
            reservations = db.execute('SELECT count(*) FROM presence_budget').fetchone()[0]
        assert reservations == len(wires) + 1
        wires.append({'reserved': reservations, 'wire_model': payload['model'], 'selected_account': 'SCRIPTED-normal-main'})
        event = {'type': 'response.completed', 'response': {'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': '{"affect":null,"thought":null}'}]}]}}
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content='data: '+json.dumps(event)+'\n\n')

    monkeypatch.setattr(aux, '_openai_http_client_kwargs', lambda *a, **kw: {
        'http_client': (httpx.AsyncClient if kw.get('async_mode') else httpx.Client)(transport=httpx.MockTransport(respond))})
    # No resolver, pool selection, auth owner, policy or bounded dispatch patches.
    runtime = resolve_runtime_provider(requested='openai-codex', target_model='gpt-6-astra-900k')
    pool = runtime['credential_pool']
    assert pool.current().id == credential.id
    assert runtime['api_key'] == credential.access_token
    assert auth.read_bytes() == original_auth
    arguments = ['--execute', '--case', 'thought_user_interest_en']
    if control == 'lower_budget':
        arguments += ['--max-calls', '3', '--case', 'thought_colleague_en']
    if control == 'stop':
        ThoughtStore().stop()
    try:
        with aux.scoped_runtime_main(None if control == 'missing_owner' else dict(runtime, model='gpt-6-astra-900k')):
            assert evaluator().main(arguments) == 0
        report = json.loads(capsys.readouterr().out)
        if control == 'stop':
            assert report['attempts_reserved'] == report['provider_entry_invocations'] == len(wires) == 0
            assert report['results'][0]['reason'] == 'stopped'
        elif control == 'missing_owner':
            assert len(wires) == 0 and report['attempts_reserved'] == 1
            assert report['results'][0]['status'] == 'not_measured'
        else:
            assert len(wires) == report['attempts_reserved'] == report['provider_entry_invocations'] == 1
            assert report['results'][0]['status'] == 'measured'
            if control == 'lower_budget':
                assert report['results'][1]['reason'] == 'budget_denied'
        assert pool.current().id == credential.id
        assert path.read_bytes() == original_config and auth.read_bytes() == original_auth
        assert not unused and bot.send_message.await_count == 0
        record_property('SCRIPTED_only', json.dumps({'control': control, 'transport_entries': wires,
            'attempts_reserved': report['attempts_reserved'], 'provider_entry_invocations': report['provider_entry_invocations'],
            'results': [{'id': r['id'], 'status': r['status'], 'reason': r.get('reason')} for r in report['results']]}))
    finally:
        h.close()

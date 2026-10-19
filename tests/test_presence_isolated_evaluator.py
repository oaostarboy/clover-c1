"""Isolated consented experiment, never enabling live presence or using live quota."""
import json
import os
from pathlib import Path

import httpx
import pytest

from agent import auxiliary_client as aux
from tests.clover_cli.test_runtime_provider_readonly import profile, inventory
from tests.test_presence_quality_evaluator import evaluator

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Secure POSIX evaluation')


def test_isolated_evaluation_uses_real_selected_owner_and_bounded_wire(monkeypatch, tmp_path):
    home, config, credential = profile(monkeypatch, tmp_path)
    quota = tmp_path / 'quota'
    quota.mkdir(mode=0o700)
    before = inventory(home), dict(os.environ)
    wires = []
    def respond(request):
        assert request.url.host == 'chatgpt.com'
        assert request.headers['chatgpt-account-id'] == 'SCRIPTED-existing-owner'
        assert request.headers['authorization'] == 'Bearer ' + credential.access_token
        payload = json.loads(request.content)
        from agent.presence_assessment import PROMPT
        assert payload['instructions'] == PROMPT
        assert payload['model'] == 'gpt-6-astra' and payload['store'] is False
        assert 'max_output_tokens' not in payload
        ledger = json.loads((quota / 'presence-eval-quota.json').read_text())
        assert ledger['used'] == len(wires) + 1
        wires.append(payload)
        event = {'type': 'response.completed', 'response': {'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': '{"affect":null,"thought":null}'}]}]}}
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content='data: '+json.dumps(event)+'\n\n')
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', lambda *a, **kw: {
        'http_client': (httpx.AsyncClient if kw.get('async_mode') else httpx.Client)(transport=httpx.MockTransport(respond))})
    module = evaluator()
    run = getattr(module, 'execute_isolated_cases', None)
    assert callable(run), 'supported consented isolated evaluator is missing'
    cases, _ = module.load_cases('normative')
    selected = [c for c in cases if c['id'] == 'thought_user_interest_en']
    results, attempts, invocations = run(selected, max_calls=3, quota_dir=quota, request_budget_consent=True)
    assert len(wires) == attempts == invocations == 1
    assert results[0]['status'] == 'measured'
    assert results[0]['raw_output'] == '{"affect":null,"thought":null}'
    assert (inventory(home), dict(os.environ)) == before
    assert not (home / 'cron').exists()


def test_isolated_cli_requires_explicit_request_budget_consent(tmp_path, capsys):
    module = evaluator()
    with pytest.raises(SystemExit) as exc:
        module.main(['--execute-isolated', '--isolated-quota-dir', str(tmp_path)])
    assert exc.value.code == 2
    assert 'request-budget' in capsys.readouterr().err


@pytest.mark.parametrize('control', ['no_consent', 'zero_budget', 'stop', 'live_store', 'managed_disabled', 'task_credentials'])
def test_isolated_gates_hold_before_reservation_or_provider(monkeypatch, tmp_path, control):
    home, config, credential = profile(monkeypatch, tmp_path)
    quota = tmp_path / 'quota'
    quota.mkdir(mode=0o700)
    if control == 'task_credentials':
        import yaml
        config['auxiliary'] = {'presence': {'api_key': 'SCRIPTED-no-task-account-override'}}
        (home/'config.yaml').write_text(yaml.safe_dump(config))
    elif control == 'zero_budget':
        import yaml
        config['proactive'] = {'aux': {'max_calls_per_day': 0}}
        (home/'config.yaml').write_text(yaml.safe_dump(config))
    elif control == 'stop': (quota/'STOP').touch()
    elif control == 'live_store':
        (home/'cron').mkdir(mode=0o700)
        (home/'cron'/'presence.db').write_bytes(b'PRIVATE-do-not-open')
    elif control == 'managed_disabled':
        (tmp_path/'managed'/'config.yaml').write_text('presence:\n  enabled: false\n')
    def forbidden(*a, **kw):
        pytest.fail('held experiment constructed transport')
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', forbidden)
    module = evaluator()
    cases, _ = module.load_cases('normative')
    results, attempts, invocations = module.execute_isolated_cases(cases[:1], max_calls=3,
        quota_dir=quota, request_budget_consent=control != 'no_consent')
    assert attempts == invocations == 0
    assert results[0]['status'] == 'not_measured'
    assert not (quota/'presence-eval-quota.json').exists()


def test_isolated_quota_never_refunds_or_resets(monkeypatch, tmp_path):
    quota = tmp_path / 'quota'
    quota.mkdir(mode=0o700)
    module = evaluator()
    assert module._reserve_isolated_call(quota, 'SCRIPTED-binding', 1)
    assert not module._reserve_isolated_call(quota, 'SCRIPTED-binding', 1)
    assert module._reserve_isolated_call(quota, 'SCRIPTED-binding', 3)
    assert module._reserve_isolated_call(quota, 'SCRIPTED-binding', 3)
    assert not module._reserve_isolated_call(quota, 'SCRIPTED-binding', 3)
    from clover_cli.readonly_files import ReadOnlyStateError
    with pytest.raises(ReadOnlyStateError):
        module._reserve_isolated_call(quota, 'DIFFERENT-owner', 3)
    assert json.loads((quota/'presence-eval-quota.json').read_text())['used'] == 3

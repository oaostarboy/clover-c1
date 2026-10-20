"""Cold setup counts against the original deadline, before and after dispatch.

Real config, credential owner, route admission, SDK and HTTPX are retained.
Only the terminal wire and loop-local elapsed-work injection are synthetic.
"""
import asyncio
import json

import httpx
import pytest
import yaml

from agent import auxiliary_client as aux
from tests.agent.presence_deadline_helpers import DeadlineClock


@pytest.mark.parametrize('stage', ['config', 'resolver', 'sdk-build', 'request-hook'])
@pytest.mark.parametrize('boundary', ['positive', 'deadline', 'cancel'])
def test_presence_cold_setup_deadline_precedes_physical_dispatch(
        monkeypatch, tmp_path, record_property, stage, boundary):
    from clover_cli import config as settings

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(home))
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-stage-owner-key')
    (home / 'config.yaml').write_text(yaml.safe_dump({
        'presence': {'budget_mode': 'requests'},
        'auxiliary': {'presence': {'provider': 'custom', 'model': 'synthetic-stage-model',
            'base_url': 'https://synthetic-stage.invalid/v1', 'key_env': 'OPENAI_API_KEY'}}}))
    requests, clients = [], []
    load_config, resolve = settings.load_config, aux.resolve_provider_client
    build = httpx.AsyncClient.build_request

    async def scenario():
        clock = DeadlineClock(monkeypatch)
        injected = False

        def checkpoint(name):
            nonlocal injected
            clock.mark(name)
            if name != stage or injected:
                return
            injected = True
            if boundary == 'deadline':
                # Model synchronous work consuming the complete original 5s
                # budget; do not restart or extend a deadline after setup.
                clock.advance(5)
            elif boundary == 'cancel':
                task = asyncio.current_task()
                assert task is not None
                task.cancel()

        def read_config(*args, **kwargs):
            result = load_config(*args, **kwargs)
            checkpoint('config')
            return result

        def resolver(*args, **kwargs):
            result = resolve(*args, **kwargs)
            checkpoint('resolver')
            return result

        def build_request(self, *args, **kwargs):
            result = build(self, *args, **kwargs)
            checkpoint('sdk-build')
            return result

        async def request_hook(request):
            checkpoint('request-hook')

        async def transport(request):
            clock.mark('physical_dispatch')
            requests.append(request)
            return httpx.Response(200, json={'choices': [
                {'message': {'content': 'synthetic success'}}]})

        def factory(base_url, *, async_mode=False):
            assert async_mode, 'bounded resolver attempted a synchronous probe'
            http = httpx.AsyncClient(transport=httpx.MockTransport(transport),
                event_hooks={'request': [request_hook]}, follow_redirects=True)
            clients.append(http)
            return {'http_client': http}

        monkeypatch.setattr(settings, 'load_config', read_config)
        monkeypatch.setattr(aux, 'resolve_provider_client', resolver)
        monkeypatch.setattr(aux, '_openai_http_client_kwargs', factory)
        monkeypatch.setattr(httpx.AsyncClient, 'build_request', build_request)
        clock.mark('completion_started')
        try:
            call = aux.presence_completion(messages=[{'role': 'user', 'content': 'synthetic evidence'}],
                                           max_output_tokens=32, timeout=5)
            if boundary == 'positive':
                assert await call == 'synthetic success'
                assert len(requests) == 1
                request = requests[0]
                assert str(request.url) == 'https://synthetic-stage.invalid/v1/chat/completions'
                assert request.headers['authorization'] == 'Bearer synthetic-stage-owner-key'
                payload = json.loads(request.content)
                assert payload['model'] == 'synthetic-stage-model'
                assert payload['max_tokens'] == 32
            else:
                with pytest.raises(TimeoutError if boundary == 'deadline' else asyncio.CancelledError):
                    await call
                assert requests == [], 'late/cancelled setup reached physical transport'
            assert injected, 'probe did not reach the claimed stage'
            assert all(client.is_closed for client in clients)
            if stage != 'config':
                assert len(clients) == 1, 'resolver was bypassed or repeated'
            await asyncio.sleep(0)
            assert len(requests) == (1 if boundary == 'positive' else 0)
            clock.mark('completion_finished')
        finally:
            record_property('deadline_stage_trace', json.dumps(clock.events))
            for client in clients:
                await client.aclose()

    asyncio.run(scenario())

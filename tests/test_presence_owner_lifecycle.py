"""Real persisted pools and bounded SDK, with only HTTP endpoints synthetic."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest

from agent import auxiliary_client as aux
from agent import presence_authority as handoff
from agent.bounded_completion import CompletionHeld
from agent.credential_pool import load_pool, PooledCredential
from clover_constants import set_clover_home_override, reset_clover_home_override
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401

CONFIG = {'model': {'provider': 'openai', 'default': 'gpt-4o-mini',
                   'base_url': 'https://api.openai.com/v1'},
          'auxiliary': {'presence': {'provider': 'auto', 'model': 'auto'}},
          'presence': {'budget_mode': 'strict_tokens'}}


class Main:
    pass


def selected_main(home):
    home.mkdir(parents=True)
    token = set_clover_home_override(home)
    try:
        row = PooledCredential(provider='openai', id='selected', label='synthetic',
            source='manual', auth_type='api_key', access_token='synthetic-' + home.name, priority=0)
        sibling = PooledCredential(provider='openai', id='unselected', label='synthetic sibling',
            source='manual', auth_type='api_key', access_token='synthetic-unselected', priority=1)
        (home/'auth.json').write_text(json.dumps({'version':1, 'authority_version':7,
            'credential_pool':{'openai':[row.to_dict(), sibling.to_dict()]}}))
        pool = load_pool('openai')
        assert pool.select().id == 'selected'
        agent = Main()
        runtime = dict(provider='openai', model='gpt-4o-mini', base_url='https://api.openai.com/v1',
                       api_mode='chat_completions', api_key=pool.current().runtime_api_key)
        agent.__dict__.update(runtime, _credential_pool=pool)
        with aux.scoped_runtime_main(dict(runtime, credential_pool=pool)):
            owner = handoff.capture(agent).bounded()
        return agent, pool, owner
    finally:
        reset_clover_home_override(token)


def forbid_selection(monkeypatch, pool):
    def forbidden(*a, **kw):
        pytest.fail('callback selected/refreshed another credential')
    monkeypatch.setattr(pool, 'select', forbidden)
    monkeypatch.setattr(pool, '_refresh_entry', forbidden)


def test_simultaneous_profile_owners_keep_exact_connections_and_restore_context(monkeypatch, tmp_path):
    a = selected_main(tmp_path/'A')
    b = selected_main(tmp_path/'B')
    for _, pool, _ in (a, b):
        forbid_selection(monkeypatch, pool)
    monkeypatch.setattr('clover_cli.config.load_config', lambda: CONFIG)
    observed = []
    async def run():
        barrier = asyncio.Event()
        async def respond(request):
            from clover_constants import get_clover_home
            home = get_clover_home()
            observed.append((home.name, request.headers['authorization']))
            if len(observed) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 1)
            assert aux._RUNTIME_MAIN_CONTEXT.get()['api_key'] == 'synthetic-' + home.name
            return httpx.Response(200, json={'choices':[{'message':{'content':'synthetic'}}]})
        monkeypatch.setattr(aux, '_openai_http_client_kwargs', lambda *a, **kw: {
            'http_client':httpx.AsyncClient(transport=httpx.MockTransport(respond))})
        async def one(home, value):
            agent, pool, owner = value
            token = set_clover_home_override(home)
            try:
                before = aux._RUNTIME_MAIN_CONTEXT.get()
                with handoff.scoped(owner, lambda: True):
                    assert aux._RUNTIME_MAIN_CONTEXT.get()['credential_pool'] is pool
                    assert await aux.presence_completion(messages=[{'role':'user','content':'synthetic'}],
                        max_output_tokens=64, timeout=2) == 'synthetic'
                assert aux._RUNTIME_MAIN_CONTEXT.get() is before
                assert handoff._ACTIVE.get() is None
            finally:
                reset_clover_home_override(token)
        await asyncio.gather(one(tmp_path/'A', a), one(tmp_path/'B', b))
    asyncio.run(run())
    assert sorted(observed) == [('A','Bearer synthetic-A'), ('B','Bearer synthetic-B')]


@pytest.mark.parametrize('change', ['deleted', 'replaced', 'expired', 'source', 'selection',
                                    'object', 'agent-route', 'lease', 'scope'])
def test_late_callback_rejects_revoked_exact_owner_without_new_selection(monkeypatch, tmp_path, change):
    home = tmp_path/'A'
    agent, pool, owner = selected_main(home)
    forbid_selection(monkeypatch, pool)
    token = set_clover_home_override(home)
    try:
        auth = home/'auth.json'
        data = json.loads(auth.read_text())
        if change == 'deleted': auth.unlink()
        elif change == 'replaced':
            data['credential_pool']['openai'][0]['access_token'] = 'synthetic-replacement'
            auth.write_text(json.dumps(data))
        elif change == 'expired':
            data['credential_pool']['openai'][0]['expires_at_ms'] = 1
            auth.write_text(json.dumps(data))
        elif change == 'source':
            data['authority_version'] += 1
            auth.write_text(json.dumps(data))
        elif change == 'selection': pool._current_id = 'unselected'
        elif change == 'object': pool._entries[0] = PooledCredential.from_dict('openai', pool.current().to_dict())
        elif change == 'agent-route': agent.base_url = 'https://unowned.invalid/v1'
        elif change == 'lease': owner = replace(owner, expires=0)
        elif change == 'scope':
            reset_clover_home_override(token)
            token = set_clover_home_override(tmp_path/'B')
        before = aux._RUNTIME_MAIN_CONTEXT.get()
        with pytest.raises(CompletionHeld):
            with handoff.scoped(owner, lambda: True):
                pytest.fail('revoked owner entered callback')
        assert aux._RUNTIME_MAIN_CONTEXT.get() is before
        assert handoff._ACTIVE.get() is None
    finally:
        reset_clover_home_override(token)


@pytest.mark.parametrize('end', ['failure', 'cancel'])
def test_callback_unwinds_active_credentials_and_next_turn_can_use_new_owner(monkeypatch, tmp_path, end):
    agent, pool, owner = selected_main(tmp_path/'A')
    forbid_selection(monkeypatch, pool)
    async def run():
        token = set_clover_home_override(tmp_path/'A')
        try:
            before = aux._RUNTIME_MAIN_CONTEXT.get()
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                with handoff.scoped(owner, lambda: True):
                    if end == 'cancel':
                        asyncio.current_task().cancel()
                        await asyncio.sleep(0)
                    raise RuntimeError('synthetic callback failure')
            assert aux._RUNTIME_MAIN_CONTEXT.get() is before
            assert handoff._ACTIVE.get() is None
            # A new callback can bind without an old turn's task-local owner.
            with handoff.scoped(owner, lambda: True):
                assert aux._RUNTIME_MAIN_CONTEXT.get()['credential_pool'] is pool
        finally:
            reset_clover_home_override(token)
    asyncio.run(run())


def test_retained_desktop_context_does_not_retain_main_credentials(monkeypatch, tmp_path):
    agent, pool, owner = selected_main(tmp_path/'A')
    with aux.scoped_runtime_main(dict(agent.__dict__, credential_pool=pool)):
        saved = handoff.callback_context()
        assert aux._RUNTIME_MAIN_CONTEXT.get()['api_key'] == 'synthetic-A'
    assert saved.get(aux._RUNTIME_MAIN_CONTEXT) is None
    assert saved.get(handoff._ACTIVE) is None
    assert 'synthetic-A' not in repr(owner)
    assert not any(isinstance(value, str) and value == 'synthetic-A' for value in vars(owner).values())

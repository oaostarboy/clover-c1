"""Mutate real stores after each final application read; never edit review probes."""
import asyncio
import json
import sqlite3

import pytest
import yaml

from tests.gateway.test_emotions_independent_review import host, seed  # noqa: F401


def setup_command(h, monkeypatch, surface):
    from agent import emotions
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tests.tui_gateway.test_emotions_validation import dispatch_reply
    from tui_gateway import server
    agent = None
    if surface == 'telegram':
        key = seed(h)
        serial = 110
        def invoke():
            nonlocal serial
            serial += 1
            events = []
            with monkeypatch.context() as mp:
                mp.setattr(h.adapter, '_enqueue_text_event', events.append)
                asyncio.run(h.adapter._handle_text_message(h.update('/emotions', message_id=serial), object()))
            return asyncio.run(h.runner._handle_emotions_command(events[0]))
    else:
        agent, transport, sid, session, send = desktop_host(h, monkeypatch)
        send('Synthetic fixture: I am frustrated.')
        session['_run_thread'].join(5)
        key = next(k for k in emotions._owners if k.conversation_root == 'desktop-stored'
                   and k.scope.profile == str(h.home.resolve()))
        serial = 0
        def invoke():
            nonlocal serial
            serial += 1
            method = 'slash.exec' if surface == 'desktop_slash' else 'command.dispatch'
            params = {'session_id': sid, **({'command': '/emotions'} if method == 'slash.exec' else {'name': 'emotions'})}
            return str(dispatch_reply(server, transport, {'jsonrpc': '2.0', 'id': f'fence-{serial}',
                                                         'method': method, 'params': params}))
    return agent, key, invoke


def mutate(h, key, race):
    from agent import emotions
    from cron.presence import ThoughtStore
    owner = emotions._owners[key][0]
    if race in {'policy', 'provider', 'managed'}:
        path = h.home / 'config.yaml'
        if race == 'managed':
            from clover_cli.managed_scope import get_managed_dir
            path = get_managed_dir() / 'config.yaml'
        config = yaml.safe_load(path.read_text()) or {}
        if race in {'policy', 'managed'}:
            config.setdefault('presence', {})['enabled'] = False
        else:
            config['auxiliary']['presence']['provider'] = 'synthetic-revoked'
        path.write_text(yaml.safe_dump(config))
    elif race == 'stop':
        ThoughtStore().stop()
    elif race == 'generation':
        ThoughtStore().advance_generation()
    elif race == 'source':
        sid, row = json.loads(owner._latest.source.source_id)
        with sqlite3.connect(h.db.db_path) as conn:
            conn.execute('UPDATE messages SET active=0 WHERE id=? AND session_id=?', (row, sid))
    elif race == 'host':
        emotions._owner_hosts.pop(owner)
    elif race == 'owner':
        emotions._owners.pop(key)
    elif race == 'sample':
        from dataclasses import replace
        owner._latest = replace(owner._latest, confidence=0.2)


@pytest.mark.parametrize('surface', ['telegram', 'desktop', 'desktop_slash'])
@pytest.mark.parametrize('boundary', ['root', 'config', 'reading', 'owner', 'evidence', 'source_current'])
@pytest.mark.parametrize('race', ['policy', 'provider', 'managed', 'stop', 'generation', 'source', 'host', 'owner', 'sample'])
def test_mutation_after_final_application_read_holds(host, monkeypatch, tmp_path, surface, boundary, race):
    from agent import emotions, emotions_config, presence_state
    from cron.presence import ThoughtStore
    policy = tmp_path / 'managed'; policy.mkdir()
    (policy / 'config.yaml').write_text('{}')
    monkeypatch.setenv('CLOVER_MANAGED_DIR', str(policy))
    agent, key, invoke = setup_command(host, monkeypatch, surface)
    owner = emotions._owners[key][0]
    sample, generation, ticket = owner._latest, owner._generation, owner._ticket
    module, name = {
        'root': (emotions, '_conversation_root'), 'config': (emotions_config, 'snapshot'),
        'reading': (emotions, '_peek_reading'), 'owner': (emotions, '_owner_current'),
        'evidence': (presence_state, 'evidence_matches'),
        'source_current': (ThoughtStore, 'source_is_current'),
    }[boundary]
    real = getattr(module, name)
    count = 0
    trigger = None
    def wrapped(*args, **kwargs):
        nonlocal count
        value = real(*args, **kwargs)
        count += 1
        if count == trigger:
            mutate(host, key, race)
        return value
    monkeypatch.setattr(module, name, wrapped)
    try:
        assert 'confidence=0.90' in invoke()
        assert count > 0
        trigger, count = count, 0
        text = invoke()
        assert count == trigger
        assert 'confidence=' not in text, (surface, boundary, race, text)
        assert (owner._generation, owner._ticket) == (generation, ticket)
        if race != 'sample':
            assert owner._latest is sample
    finally:
        if agent:
            agent.close()


@pytest.mark.parametrize('surface', ['telegram', 'desktop', 'desktop_slash'])
def test_transport_revoked_at_last_revision_observation(host, monkeypatch, surface):
    from agent.emotions_snapshot import ReadFence
    from tui_gateway import server
    agent, key, invoke = setup_command(host, monkeypatch, surface)
    real = ReadFence.seal
    def seal(fence):
        real(fence)
        if surface == 'telegram':
            monkeypatch.setattr(host.runner, '_is_user_authorized', lambda source: False)
        else:
            for session in server._sessions.values():
                if session.get('agent') is agent:
                    session['transport'].closed = True
    monkeypatch.setattr(ReadFence, 'seal', seal)
    try:
        assert 'confidence=' not in invoke()
    finally:
        if agent:
            agent.close()


@pytest.mark.parametrize('surface', ['telegram', 'desktop', 'desktop_slash'])
def test_final_owner_predicate_performs_no_io(host, monkeypatch, surface):
    from agent.emotions_snapshot import ReadFence
    from pathlib import Path
    import sqlite3
    agent, key, invoke = setup_command(host, monkeypatch, surface)
    real = ReadFence.current
    def checked(fence, **kwargs):
        def forbidden(*a, **kw):
            raise AssertionError('I/O after final fence')
        with monkeypatch.context() as mp:
            mp.setattr(Path, 'resolve', forbidden)
            mp.setattr(Path, 'stat', forbidden)
            mp.setattr(Path, 'read_text', forbidden)
            mp.setattr(sqlite3, 'connect', forbidden)
            return real(fence, **kwargs)
    monkeypatch.setattr(ReadFence, 'current', checked)
    try:
        assert 'confidence=0.90' in invoke()
    finally:
        if agent:
            agent.close()

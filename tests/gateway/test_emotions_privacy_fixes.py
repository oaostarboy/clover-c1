"""Synthetic privacy fixtures through real command entrypoints, no live affect."""
import asyncio
import pytest
from tests.gateway.test_emotions_independent_review import host, seed, capture_command  # noqa: F401
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


@pytest.mark.parametrize('field,value', [('text', '/emotions set happy'),
    ('user_id', 'different-user'), ('chat_id', 'different-chat'), ('thread_id', 'different-topic'),
    ('profile', 'different-profile'), ('transport_profile', 'different-transport')])
def test_command_mint_binds_original_fields(host, monkeypatch, field, value):
    seed(host)
    event = capture_command(host, monkeypatch)
    setattr(event if field == 'text' else event.source, field, value)
    text = asyncio.run(host.runner._handle_emotions_command(event))
    assert 'Provider route:' not in text


def test_separate_platform_duplicate_cannot_redisclose_but_new_message_works(host, monkeypatch):
    seed(host)
    first = capture_command(host, monkeypatch)
    duplicate = capture_command(host, monkeypatch)  # same real platform delivery id
    assert 'confidence=0.90' in asyncio.run(host.runner._handle_emotions_command(first))
    assert 'Provider route:' not in asyncio.run(host.runner._handle_emotions_command(duplicate))
    events = []
    with monkeypatch.context() as mp:
        mp.setattr(host.adapter, '_enqueue_text_event', events.append)
        asyncio.run(host.adapter._handle_text_message(host.update('/emotions', message_id=102), object()))
    assert 'confidence=0.90' in asyncio.run(host.runner._handle_emotions_command(events[0]))


@pytest.mark.parametrize('surface', ['telegram', 'desktop'])
@pytest.mark.parametrize('phase', ['before', 'after'])
@pytest.mark.parametrize('change', ['missing', 'invalid', 'deleted'])
def test_unprovable_root_withholds_without_owner_repair(host, monkeypatch, surface, phase, change):
    from agent import emotions
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tests.gateway.test_emotions_independent_review import rpc
    from tui_gateway import server
    if surface == 'telegram':
        key = seed(host)
        event = capture_command(host, monkeypatch)
        entry = host.runner.session_store.lookup_by_session_key(host.runner.session_store._generate_session_key(event.source))
        stored = entry.session_id
        invoke = lambda: asyncio.run(host.runner._handle_emotions_command(event))
        agent = None
    else:
        agent, transport, sid, session, send = desktop_host(host, monkeypatch)
        send('Synthetic fixture: I am frustrated.')
        key = next(k for k in emotions._owners if k.conversation_root == 'desktop-stored')
        stored = session['session_key']
        invoke = lambda: str(rpc(server, transport, sid))
    owner = emotions._owners[key][0]
    original = owner._latest
    def mutate():
        if change == 'deleted':
            import sqlite3
            # Simulate a deleted/broken durable root independently of the
            # production deletion owner, leaving dangling fixture evidence.
            with sqlite3.connect(host.db.db_path) as db:
                db.execute('DELETE FROM sessions WHERE id=?', (stored,))
        else:
            monkeypatch.setattr(host.db, 'get_conversation_root', lambda sid: None if change == 'missing' else [])
    real = emotions.render
    if phase == 'before':
        mutate()
    else:
        def render(**kw):
            result = real(**kw)
            mutate()
            return result
        monkeypatch.setattr(emotions, 'render', render)
    try:
        assert 'confidence=' not in invoke()
        assert owner._latest is original
    finally:
        if agent: agent.close()


@pytest.mark.parametrize('surface', ['telegram', 'desktop'])
@pytest.mark.parametrize('race', ['owner_removed', 'owner_changed', 'host_owner_changed', 'policy_off', 'provider_changed', 'stop', 'generation', 'source_deleted'])
def test_final_private_read_rechecks_owner_policy_provider_and_source(host, monkeypatch, surface, race):
    import yaml
    from agent import emotions
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tests.gateway.test_emotions_independent_review import rpc
    from tui_gateway import server
    from cron.presence import ThoughtStore
    if surface == 'telegram':
        key = seed(host)
        event = capture_command(host, monkeypatch)
        invoke = lambda: asyncio.run(host.runner._handle_emotions_command(event))
        agent = None
    else:
        agent, transport, sid, session, send = desktop_host(host, monkeypatch)
        send('Synthetic fixture: I am frustrated.')
        key = next(k for k in emotions._owners if k.conversation_root == 'desktop-stored' and k.scope.profile == str(host.home.resolve()))
        invoke = lambda: str(rpc(server, transport, sid))
    owner = emotions._owners[key][0]
    original = owner._latest
    real = emotions.render
    def render(**kw):
        text = real(**kw)
        assert 'confidence=0.90' in text
        if race == 'owner_removed': emotions._owners.pop(key)
        elif race == 'owner_changed':
            from copy import copy
            emotions._owners[key] = (copy(owner), emotions.clock())
        elif race == 'host_owner_changed':
            from copy import copy
            emotions._owner_hosts[owner]().state.affect = copy(owner)
        elif race in {'policy_off', 'provider_changed'}:
            cfg = yaml.safe_load((host.home/'config.yaml').read_text())
            if race == 'policy_off': cfg['presence']['enabled'] = False
            else: cfg['auxiliary']['presence']['provider'] = 'foreign-synthetic-provider'
            (host.home/'config.yaml').write_text(yaml.safe_dump(cfg))
        elif race == 'stop': ThoughtStore().stop()
        elif race == 'generation': ThoughtStore().advance_generation()
        else:
            import json, sqlite3
            stored, row = json.loads(original.source.source_id)
            with sqlite3.connect(host.db.db_path) as db:
                db.execute('UPDATE messages SET active=0 WHERE id=? AND session_id=?', (row, stored))
        return text
    monkeypatch.setattr(emotions, 'render', render)
    try:
        assert 'confidence=' not in invoke()
        assert owner._latest is original
    finally:
        if agent: agent.close()


def test_generic_submission_proof_is_not_command_authority(host, monkeypatch):
    from agent.workstream_origin import mint_human_ingress
    from dataclasses import replace
    seed(host)
    event = replace(capture_command(host, monkeypatch))
    assert mint_human_ingress(event, platform='telegram')
    text = asyncio.run(host.runner._handle_emotions_command(event))
    assert 'Provider route:' not in text


def test_final_evidence_read_tombstone_race_holds_without_owner_mutation(host, monkeypatch):
    from agent import emotions
    key = seed(host)
    owner = emotions._owners[key][0]
    h = emotions._owner_hosts[owner]()
    reading = owner._latest
    reader_type = type(h.reader)
    original = reader_type.__call__
    calls = []
    def changed(reader, key, ref):
        value = original(reader, key, ref)
        calls.append(True)
        if len(calls) == 2:
            h.store.invalidate_source(owner.key, ref, 'invalidated', emotions.clock())
        return value
    monkeypatch.setattr(reader_type, '__call__', changed)
    event = capture_command(host, monkeypatch)
    text = asyncio.run(host.runner._handle_emotions_command(event))
    assert len(calls) >= 2
    assert 'confidence=' not in text
    assert owner._latest is reading


@pytest.mark.parametrize('surface', ['telegram', 'desktop'])
def test_scope_rechecked_after_last_database_read(host, monkeypatch, surface):
    from agent import emotions
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tests.gateway.test_emotions_independent_review import rpc
    from tui_gateway import server
    if surface == 'telegram':
        seed(host)
        event = capture_command(host, monkeypatch)
        invoke = lambda: asyncio.run(host.runner._handle_emotions_command(event))
        revoke = lambda: monkeypatch.setattr(host.runner, '_is_user_authorized', lambda source: False)
        agent = None
    else:
        agent, transport, sid, session, send = desktop_host(host, monkeypatch)
        send('Synthetic fixture: I am frustrated.')
        invoke = lambda: str(rpc(server, transport, sid))
        revoke = lambda: setattr(transport, 'closed', True)
    real = emotions._conversation_root
    calls = []
    def root_after_revoke(*args, **kwargs):
        value = real(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2: revoke()
        return value
    monkeypatch.setattr(emotions, '_conversation_root', root_after_revoke)
    try:
        assert 'confidence=' not in invoke()
        assert len(calls) == 2
    finally:
        if agent: agent.close()


def test_zero_budget_is_not_reported_as_consumed_quota(host):
    import yaml
    from agent.emotions import render
    seed(host)
    cfg = yaml.safe_load((host.home/'config.yaml').read_text())
    cfg['proactive'].setdefault('aux', {})['max_calls_per_day'] = 0
    (host.home/'config.yaml').write_text(yaml.safe_dump(cfg))
    assert 'reason: zero_assessment_budget' in render()


@pytest.mark.parametrize('surface', ['telegram', 'desktop', 'desktop_slash'])
@pytest.mark.parametrize('confidence', [0.9, 0.2])
def test_warm_status_avoids_startup_loaders_token_flush_and_preserves_owner(host, monkeypatch, surface, confidence):
    from agent import emotions
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tests.gateway.test_emotions_independent_review import rpc
    from tui_gateway import server
    from clover_cli import config
    if surface == 'telegram':
        key = seed(host)
        event = capture_command(host, monkeypatch)
        invoke = lambda: asyncio.run(host.runner._handle_emotions_command(event))
        agent = None
    else:
        agent, transport, sid, session, send = desktop_host(host, monkeypatch)
        send('Synthetic fixture: I am frustrated.')
        key = next(k for k in emotions._owners if k.conversation_root == 'desktop-stored' and k.scope.profile == str(host.home.resolve()))
        invoke = lambda: str(rpc(server, transport, sid))
    # The normal turn emits message.complete before its post-turn info/DB work
    # finishes. Isolate command effects from that legitimate background owner.
    import threading
    if surface != 'telegram':
        session['_run_thread'].join(5)
        assert not session['_run_thread'].is_alive()
    for worker in threading.enumerate():
        if worker.name == 'auto-title': worker.join(5)
    if surface == 'desktop_slash':
        from tests.tui_gateway.test_emotions_validation import dispatch_reply
        invoke = lambda: str(dispatch_reply(server, transport, {'jsonrpc':'2.0', 'id':'warm',
            'method':'slash.exec', 'params':{'session_id':sid, 'command':'/emotions'}}))
    owner = emotions._owners[key][0]
    from dataclasses import replace
    owner._latest = replace(owner._latest, confidence=confidence)  # Synthetic sample only.
    sample, generation, ticket = owner._latest, owner._generation, owner._ticket
    h = emotions._owner_hosts[owner]()
    def cached_state():
        return {k:repr(v) for k,v in vars(h.agent).items() if 'cache' in k or 'history' in k or k in {'messages','system_prompt'}}
    before = cached_state()
    forbidden_calls = []
    def forbidden(*a, **kw):
        import traceback, threading
        forbidden_calls.append((threading.current_thread().name, traceback.format_stack(limit=6)))
        raise AssertionError('status called mutating startup/write path')
    with monkeypatch.context() as mp:
        for name in ('load_config', 'load_config_readonly', 'ensure_clover_home', 'migrate_config'):
            mp.setattr(config, name, forbidden)
        mp.setattr(host.db, 'flush_token_counts', forbidden)
        text = invoke()
    try:
        assert f'confidence={confidence:.2f}' in text, text
        if confidence < 0.7:
            assert 'posture: ordinary' in text and 'tentative valence=' in text
        assert not forbidden_calls, '\n'.join(name + '\n' + ''.join(stack) for name, stack in forbidden_calls)
        assert (owner._latest, owner._generation, owner._ticket) == (sample, generation, ticket)
        assert before == cached_state()
        if surface != 'telegram':
            send('Synthetic normal dialogue continues after status.')
            assert session['history'][-1]['role'] == 'assistant'
            session['_run_thread'].join(5)
        else:
            host.inbound_human('Synthetic normal dialogue continues after status.', message_id=103)
            assert host.results[-1]['final_response'] == 'ack'
    finally:
        if agent: agent.close()

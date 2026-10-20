"""Independent bounded synthetic probes; production remains untouched."""
import asyncio
import json
import sqlite3
import pytest
import yaml
from tests.gateway.test_emotions_independent_review import host, seed, capture_command, rpc  # noqa: F401


@pytest.mark.parametrize('surface', ['telegram', 'desktop', 'desktop_slash'])
@pytest.mark.parametrize('race', ['unchanged', 'policy_off', 'provider_changed', 'stop', 'generation', 'source_deleted', 'owner_removed'])
def test_final_root_io_cannot_disclose_revoked_reading(host, monkeypatch, surface, race):
    from agent import emotions
    from cron.presence import ThoughtStore
    from tui_gateway import server
    from tests.tui_gateway.test_presence_integration import desktop_host
    agent = None
    if surface == 'telegram':
        key = seed(host)
        event = capture_command(host, monkeypatch)
        invoke = lambda: asyncio.run(host.runner._handle_emotions_command(event))
    else:
        agent, transport, sid, session, send = desktop_host(host, monkeypatch)
        send('Synthetic fixture: I am frustrated.')
        session['_run_thread'].join(5)
        key = next(k for k in emotions._owners if k.conversation_root == 'desktop-stored' and k.scope.profile == str(host.home.resolve()))
        if surface == 'desktop':
            invoke = lambda: str(rpc(server, transport, sid))
        else:
            from tests.tui_gateway.test_emotions_validation import dispatch_reply
            invoke = lambda: str(dispatch_reply(server, transport, {'jsonrpc':'2.0','id':'lastio','method':'slash.exec','params':{'session_id':sid,'command':'/emotions'}}))
    owner = emotions._owners[key][0]
    sample = owner._latest
    assert sample is not None
    real = emotions._conversation_root
    calls = []
    def root(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            if race in {'policy_off', 'provider_changed'}:
                cfg = yaml.safe_load((host.home/'config.yaml').read_text())
                if race == 'policy_off': cfg['presence']['enabled'] = False
                else: cfg['auxiliary']['presence']['provider'] = 'synthetic-foreign'
                (host.home/'config.yaml').write_text(yaml.safe_dump(cfg))
            elif race == 'stop': ThoughtStore().stop()
            elif race == 'generation': ThoughtStore().advance_generation()
            elif race == 'owner_removed': emotions._owners.pop(key)
            elif race == 'source_deleted':
                stored, row = json.loads(sample.source.source_id)
                with sqlite3.connect(host.db.db_path) as conn:
                    conn.execute('UPDATE messages SET active=0 WHERE id=? AND session_id=?', (row, stored))
        return result
    monkeypatch.setattr(emotions, '_conversation_root', root)
    try:
        text = invoke()
        assert len(calls) == 2
        assert owner._latest is sample
        if race == 'unchanged':
            assert 'confidence=0.90' in text, text
        else:
            assert 'confidence=' not in text, f'{surface}/{race}: stale disclosure after last root I/O: {text}'
    finally:
        if agent: agent.close()

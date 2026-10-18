"""Remaining read-only command controls using real offline surface handlers."""
import asyncio
import io
import sqlite3
import pytest
import yaml
from rich.console import Console
from tests.gateway.test_emotions_independent_review import host, seed, capture_command, rpc  # noqa: F401


@pytest.mark.parametrize('argument',['on','off','set happy'])
def test_actual_telegram_argument_parity(host,monkeypatch,argument):
    events=[]
    monkeypatch.setattr(host.adapter,'_enqueue_text_event',events.append)
    asyncio.run(host.adapter._handle_text_message(host.update('/emotions '+argument,message_id=301),object()))
    assert len(events)==1
    text=asyncio.run(host.runner._handle_emotions_command(events[0]))
    assert 'takes no arguments' in text
    assert 'Provider route:' not in text
    assert not host.requests


@pytest.mark.parametrize('surface',['telegram','desktop'])
def test_cyclic_root_holds_without_changing_owner(host,monkeypatch,surface):
    from agent import emotions
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    agent=None
    if surface=='telegram':
        key=seed(host)
        event=capture_command(host,monkeypatch)
        entry=host.runner.session_store.lookup_by_session_key(host.runner.session_store._generate_session_key(event.source))
        sid=entry.session_id
        invoke=lambda:asyncio.run(host.runner._handle_emotions_command(event))
    else:
        agent,transport,runtime_id,session,send=desktop_host(host,monkeypatch)
        send('Synthetic fixture: I am frustrated.')
        session['_run_thread'].join(5)
        sid=session['session_key']
        key=next(k for k in emotions._owners if k.conversation_root==sid and k.scope.profile==str(host.home.resolve()))
        invoke=lambda:str(rpc(server,transport,runtime_id))
    owner=emotions._owners[key][0]
    before=(owner._latest,owner.key,owner._generation,owner._ticket)
    with sqlite3.connect(host.db.db_path) as conn:
        conn.execute('UPDATE sessions SET parent_session_id=? WHERE id=?',(sid,sid))
    try:
        assert 'confidence=' not in invoke()
        assert before==(owner._latest,owner.key,owner._generation,owner._ticket)
    finally:
        if agent:agent.close()


@pytest.mark.parametrize('disabled',[True,False])
def test_actual_cli_stop_is_profile_local_and_disabled_is_honest(host,monkeypatch,tmp_path,disabled):
    from clover_constants import set_clover_home_override,reset_clover_home_override
    from cron.presence import ThoughtStore
    from cli import CloverCLI
    ThoughtStore().stop()
    cfg=yaml.safe_load((host.home/'config.yaml').read_text())
    cfg['presence']['enabled']=not disabled
    (host.home/'config.yaml').write_text(yaml.safe_dump(cfg))
    def output():
        out=io.StringIO();cli=CloverCLI.__new__(CloverCLI);cli.console=Console(file=out,width=180)
        assert cli.process_command('/emotions') is True
        return out.getvalue()
    text=output()
    assert 'confidence=' not in text
    if disabled:
        assert 'Emotions: disabled' in text and 'reason: feature_disabled' in text
    else:
        assert 'Emotions: enabled' in text and 'reason: stopped' in text
    other=tmp_path/'different-profile';other.mkdir()
    (other/'config.yaml').write_text(yaml.safe_dump({'presence':{'enabled':True}}))
    token=set_clover_home_override(other)
    try:
        assert 'reason: stopped' not in output()
        assert not (other/'cron/presence.db').exists()
    finally:
        reset_clover_home_override(token)
    assert not host.requests

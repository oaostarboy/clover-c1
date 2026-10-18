"""Independent offline adversarial review of fcecde38; all affect is synthetic.

Expected-security assertions intentionally fail on candidate gaps. Production
code is never replaced; existing harness scripts only provider/transport seams.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
import importlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import threading

import pytest
import yaml
from rich.console import Console
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_emotions_command import inbound_command
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


@pytest.fixture(autouse=True)
def clear_ephemeral_owners():
    from agent import emotions
    emotions._owners.clear()
    yield
    emotions._owners.clear()


@pytest.fixture
def host(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    h.requests = requests
    yield h
    h.close()


def seed(h):
    h.inbound_human('Synthetic fixture: I am frustrated. PRIVATE-REVIEW-EVIDENCE.', message_id=100)
    from agent.emotions import _owners
    return next(k for k in _owners if k.scope.profile == str(h.home.resolve()))


def capture_command(h, monkeypatch, *, shared=False):
    captured = []
    with monkeypatch.context() as mp:
        mp.setattr(h.adapter, '_enqueue_text_event', captured.append)
        update = h.update('/emotions', message_id=101)
        if shared:
            update.message.chat.type = 'group'
        asyncio.run(h.adapter._handle_text_message(update, object()))
    assert len(captured) == 1
    return captured[0]


def rpc(server, transport, sid, **extra):
    return server.dispatch({'jsonrpc':'2.0', 'id':'review', 'method':'command.dispatch',
        'params':{'session_id':sid, 'name':'emotions', **extra}}, transport=transport)


def test_import_origins_and_registry_catalog(host):
    root = Path(__file__).resolve().parents[2]
    modules = ['agent.emotions', 'agent.presence_state', 'agent.workstream_origin',
               'agent.presence_runtime', 'gateway.run', 'cli', 'clover_cli.commands',
               'tui_gateway.server', 'tui_gateway.methods_tools']
    origins = {n:str(Path(importlib.import_module(n).__file__).resolve()) for n in modules}
    assert all(Path(p).is_relative_to(root) for p in origins.values()), origins
    from clover_cli.commands import COMMANDS, telegram_bot_commands, resolve_command
    assert '/emotions' in COMMANDS
    assert any(row[0] == 'emotions' for row in telegram_bot_commands())
    assert resolve_command('emotions').busy_policy == 'dispatch'
    from tui_gateway import server
    catalog = server.dispatch({'id':'catalog', 'method':'commands.catalog', 'params':{}})
    assert '/emotions' in json.dumps(catalog)
    print('REVIEW_IMPORT_ORIGINS', json.dumps({'python':sys.executable,'modules':origins}))


def test_actual_telegram_read_preserves_db_generation_history_and_no_dispatch(host, monkeypatch):
    h = host
    seed(h)
    from cron.presence import ThoughtStore
    for worker in threading.enumerate():
        if worker.name == 'auto-title': worker.join(5)
    generation = ThoughtStore().generation()
    paths = [h.db_path, ThoughtStore().path]
    def dumps():
        result=[]
        for p in paths:
            with sqlite3.connect(p) as db: result.append(list(db.iterdump()))
        return result
    before = dumps()
    config = (h.home/'config.yaml').read_bytes()
    calls = (len(h.requests), h.scripted.calls, len(h.results))
    from agent import auxiliary_client as aux
    attempts=[]
    def forbidden(*a, **kw):
        attempts.append(True)
        raise AssertionError('credential or provider call from status')
    monkeypatch.setattr(aux, 'resolve_provider_client', forbidden)
    for name in ['call_llm','async_call_llm','presence_completion']:
        monkeypatch.setattr(aux, name, forbidden)
    text = inbound_command(h, '/emotions')
    assert 'confidence=0.90' in text and 'PRIVATE-REVIEW-EVIDENCE' not in text
    assert 'Reason: bounded current authenticated evidence' in text
    assert not attempts
    assert calls == (len(h.requests), h.scripted.calls, len(h.results))
    assert ThoughtStore().generation() == generation
    assert dumps() == before
    assert (h.home/'config.yaml').read_bytes() == config
    print('SYNTHETIC_REVIEW_OUTPUT', text)


def test_actual_desktop_read_preserves_running_task_and_context(host, monkeypatch):
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    from cron.presence import ThoughtStore
    h=host
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    try:
        send('Synthetic fixture: I am frustrated. Be brief.')
        before=(ThoughtStore().generation(), len(h.requests), json.dumps(session['history']), session['history_version'])
        session['running']=True
        result=rpc(server, transport, sid)
        assert 'careful_brief' in result['result']['output'], result
        assert session['running'] is True
        assert before == (ThoughtStore().generation(),len(h.requests),json.dumps(session['history']),session['history_version'])
    finally:
        session['running']=False
        agent.close()


def test_replayed_real_telegram_event_cannot_redisclose(host, monkeypatch):
    seed(host)
    event=capture_command(host,monkeypatch)
    first=asyncio.run(host.runner._handle_message(event))
    assert 'confidence=0.90' in first, first
    replay=asyncio.run(host.runner._handle_message(event))
    assert 'confidence=' not in (replay or ''), ('REPLAY DISCLOSED',replay)


def test_group_capability_cannot_be_retargeted_to_private(host,monkeypatch):
    seed(host)
    event=capture_command(host,monkeypatch,shared=True)
    event.source.chat_type='dm'
    # Same object identity is not sufficient: the minted audience was shared.
    text=asyncio.run(host.runner._handle_message(event))
    assert 'confidence=' not in (text or ''), ('GROUP TO PRIVATE DISCLOSED',text)


@pytest.mark.parametrize('race',['authorization','conversation_root','source_principal'])
def test_telegram_readback_rechecks_current_owner(host,monkeypatch,race):
    h=host
    seed(h)
    event=capture_command(h,monkeypatch)
    from agent import emotions
    real=emotions.render
    def change(**kw):
        text=real(**kw)
        if race=='authorization': monkeypatch.setattr(h.runner,'_is_user_authorized',lambda s:False)
        elif race=='source_principal': event.source.user_id='foreign-review-user'
        else:
            entry=h.runner.session_store.lookup_by_session_key(h.runner.session_store._generate_session_key(event.source))
            entry.session_id='foreign-review-root'
        return text
    monkeypatch.setattr(emotions,'render',change)
    text=asyncio.run(h.runner._handle_emotions_command(event))
    assert 'confidence=' not in text, ('REVOKED OWNER DISCLOSED',race,text)


@pytest.mark.parametrize('race',['stored_session_key','db_conversation_root'])
def test_desktop_readback_rechecks_conversation_root(host,monkeypatch,race):
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    from agent import emotions
    h=host
    agent,transport,sid,session,send=desktop_host(h,monkeypatch)
    try:
        send('Synthetic fixture: I am frustrated. Be brief.')
        real=emotions.render
        def change(**kw):
            text=real(**kw)
            if race=='stored_session_key':session['session_key']='different-review-root'
            else:monkeypatch.setattr(h.db,'get_conversation_root',lambda key:'different-review-root')
            return text
        monkeypatch.setattr(emotions,'render',change)
        result=rpc(server,transport,sid)
        assert 'confidence=' not in json.dumps(result), ('ROOT RACE DISCLOSED',race,result)
    finally:agent.close()


@pytest.mark.parametrize('sid',[None,[],{},17,'','foreign','desktop-runtime\u200b','desktop-runtime\n','é'*600])
def test_actual_desktop_malformed_or_unicode_id_fails_closed(host,monkeypatch,sid):
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    agent,transport,real_sid,session,send=desktop_host(host,monkeypatch)
    try:
        result=rpc(server,transport,sid)
        assert 'Provider route:' not in json.dumps(result), result
    finally:agent.close()


@pytest.mark.parametrize('field',['profile','project','audience','root','conversation_root'])
def test_exact_scope_no_aliasing(host,field):
    from agent.emotions import render
    key=seed(host)
    foreign=(replace(key,conversation_root='foreign') if field=='conversation_root' else
             replace(key,scope=replace(key.scope,**{field:'foreign'})))
    assert 'confidence=' not in render(key=foreign)


def test_expired_restart_disabled_are_unknown_without_inference(host,monkeypatch):
    from agent import emotions, presence_runtime
    key=seed(host)
    before=len(host.requests)
    now=presence_runtime.clock()
    monkeypatch.setattr(presence_runtime,'clock',lambda:now+timedelta(seconds=121))
    text=emotions.render(key=key)
    assert 'unknown (expired)' in text and 'confidence=' not in text
    assert key not in emotions._owners
    emotions._owners.clear()  # same ephemeral loss as restart; no persisted reading
    assert 'unknown (no recent reading)' in emotions.render(key=key)
    cfg=yaml.safe_load((host.home/'config.yaml').read_text());cfg['presence']['enabled']=False
    (host.home/'config.yaml').write_text(yaml.safe_dump(cfg))
    text=emotions.render(key=key)
    assert 'Emotions: disabled' in text and 'confidence=' not in text
    assert len(host.requests)==before


@pytest.mark.parametrize('kind',['malformed_managed','managed_off','route_invalid','malformed_presence'])
def test_policy_and_malformed_config_no_model(host,monkeypatch,tmp_path,kind):
    from agent.emotions import render
    cfg=yaml.safe_load((host.home/'config.yaml').read_text())
    if kind in {'malformed_managed','managed_off'}:
        managed=tmp_path/'managed';managed.mkdir()
        (managed/'config.yaml').write_text('presence: [' if kind=='malformed_managed' else 'presence:\n  enabled: false\n')
        monkeypatch.setenv('CLOVER_MANAGED_DIR',str(managed))
    elif kind=='route_invalid': cfg['auxiliary']['presence']['model']={'not':'literal'}
    else:cfg['presence']='invalid'
    (host.home/'config.yaml').write_text(yaml.safe_dump(cfg))
    text=render()
    assert not host.requests
    if kind in {'malformed_managed','malformed_presence'}:assert 'unavailable' in text,text
    elif kind=='managed_off':assert 'Emotions: disabled' in text,text
    else:assert 'configured_provider_and_model_required' in text,text


@pytest.mark.parametrize('hold',['stopped','daily_request_quota_exhausted'])
def test_cli_reads_actual_profile_hold_without_owner_key(host,hold):
    from agent.emotions import render
    from cron.presence import ThoughtStore
    store=ThoughtStore()
    if hold=='stopped':store.stop()
    else:
        from agent.presence_runtime import clock
        for i in range(3):
            assert store.reserve_assessment(event_id=f'review-{i}',thought_id=f'review-{i}',revision=1,
                day=clock().date().isoformat(),max_calls=3,tokens=10)
    text=render()
    assert f'reason: {hold}' in text,text
    assert 'eligible only after all gates' not in text,text


def test_real_cli_rejects_arguments_in_parity(host):
    from cli import CloverCLI
    output=io.StringIO();cli=CloverCLI.__new__(CloverCLI);cli.console=Console(file=output,width=150)
    assert cli.process_command('/emotions set happy') is True
    assert 'takes no arguments' in output.getvalue(),output.getvalue()
    assert not host.requests


def test_route_redaction_and_dynamic_inheritance(host):
    from agent.emotions import render
    cfg=yaml.safe_load((host.home/'config.yaml').read_text())
    cfg['auxiliary']={'presence':{'provider':'auto','model':'auto'}}
    cfg['model']={'provider':'openai','default':'synthetic-first'}
    (host.home/'config.yaml').write_text(yaml.safe_dump(cfg))
    assert 'openai / synthetic-first' in render()
    cfg['model']={'provider':'anthropic','default':'synthetic-second'}
    (host.home/'config.yaml').write_text(yaml.safe_dump(cfg))
    assert 'anthropic / synthetic-second' in render()
    cfg['model']={'provider':'openai','default':'sk-proj-'+'s'*64}
    (host.home/'config.yaml').write_text(yaml.safe_dump(cfg))
    text=render()
    assert 's'*64 not in text and 'PRIVATE-REVIEW-EVIDENCE' not in text,text
    assert not host.requests



def test_real_telegram_busy_dispatch_does_not_interrupt_or_advance_generation(host,monkeypatch):
    from cron.presence import ThoughtStore
    from types import SimpleNamespace
    from unittest.mock import Mock
    import time
    seed(host)
    event=capture_command(host,monkeypatch)
    key=host.runner.session_store._generate_session_key(event.source)
    active=SimpleNamespace(interrupt=Mock(),get_activity_summary=lambda:{'seconds_since_activity':0})
    host.runner._running_agents[key]=active
    host.runner._running_agents_ts[key]=time.time()
    before=(ThoughtStore().generation(),len(host.requests),dict(host.runner._session_run_generation))
    try:
        text=asyncio.run(host.runner._handle_message(event))
        assert 'confidence=0.90' in text,text
        assert not active.interrupt.called
        assert host.runner._is_session_running(key)
        assert before==(ThoughtStore().generation(),len(host.requests),dict(host.runner._session_run_generation))
    finally:host.runner._release_running_agent_state(key)


@pytest.mark.parametrize('kind',['shared','internal','unminted','unauthorized'])
def test_real_gateway_denies_nonprivate_or_forged_commands(host,monkeypatch,kind):
    seed(host)
    event=capture_command(host,monkeypatch,shared=(kind=='shared'))
    if kind=='internal':event.internal=True
    elif kind=='unminted':
        from dataclasses import replace
        event=replace(event)
    elif kind=='unauthorized':
        monkeypatch.setattr(host.runner,'_is_user_authorized',lambda s:False)
    text=asyncio.run(host.runner._handle_message(event))
    assert 'confidence=' not in (text or '') and 'Provider route:' not in (text or ''),text
    assert len(host.requests)==1


def test_real_cli_autocomplete_and_help(host, capsys):
    from clover_cli.commands import SlashCommandCompleter,gateway_help_lines
    from prompt_toolkit.document import Document
    words=[c.text for c in SlashCommandCompleter().get_completions(Document('/emo'),None)]
    assert 'emotions' in words,words  # completer replaces text after the slash
    assert 'emotions' in '\n'.join(gateway_help_lines())
    from cli import CloverCLI
    output=io.StringIO();cli=CloverCLI.__new__(CloverCLI);cli.console=Console(file=output,width=160)
    cli.config={}
    cli.show_help('emotions')
    assert 'emotions' in capsys.readouterr().out.lower()


def test_fresh_off_command_does_not_create_prompt_files_or_import_inference(host,tmp_path):
    import subprocess
    home=tmp_path/'cold-off';home.mkdir()
    (home/'config.yaml').write_text('presence:\n  enabled: false\nmodel:\n  provider: openai\n  default: synthetic-model\n')
    source=Path(__file__).resolve().parents[2]
    code="""import socket,sys,json,pathlib
attempts=[]
def deny(*a,**kw):
 attempts.append(str(a[:1]));raise AssertionError('network forbidden')
socket.socket.connect=deny;socket.getaddrinfo=deny
from agent.emotions import render
before={str(p.relative_to(pathlib.Path.home())):p.read_bytes().hex() for p in pathlib.Path.home().rglob('*') if p.is_file()}
modules=set(sys.modules)
text=render()
after={str(p.relative_to(pathlib.Path.home())):p.read_bytes().hex() for p in pathlib.Path.home().rglob('*') if p.is_file()}
print(json.dumps({'before':before,'after':after,'text':text,'network_attempts':attempts,'introduced_modules':sorted(set(sys.modules)-modules)}))
"""
    result=subprocess.run([sys.executable,'-c',code],cwd=source,
        env={'PATH':'/usr/bin:/bin','HOME':str(home),'CLOVER_HOME':str(home),'PYTHONPATH':str(source)},
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    data=json.loads(result.stdout)
    assert 'Emotions: disabled' in data['text'] and not data['network_attempts']
    assert not any(p.endswith('.db') for p in data['after'])
    assert data['before']==data['after'],('OFF COMMAND WROTE FILES',list(data['before']),list(data['after']))
    assert 'agent.auxiliary_client' not in data['introduced_modules'],data['introduced_modules']



def test_real_telegram_command_during_inflight_provider_does_not_cancel_task(host):
    from cron.presence import ThoughtStore
    h=host
    entered=threading.Event();release=threading.Event()
    real=h.scripted.client.chat.completions.create.side_effect
    def blocked(*a,**kw):
        entered.set()
        assert release.wait(10),'review provider gate timed out'
        return real(*a,**kw)
    h.scripted.client.chat.completions.create.side_effect=blocked
    async def run():
        baseline=h.adapter.send.await_count
        await h.adapter._handle_text_message(h.update('Synthetic ordinary in-flight work',message_id=110),object())
        assert await asyncio.to_thread(entered.wait,5),'real conversation did not reach provider seam'
        generation=ThoughtStore().generation()
        try:
            await h.adapter._handle_text_message(h.update('/emotions',message_id=111),object())
            deadline=asyncio.get_running_loop().time()+5
            text=''
            while 'Emotions:' not in text:
                messages=[(a.args[1] if len(a.args)>1 else a.kwargs.get('content',''))
                          for a in h.adapter.send.await_args_list[baseline:]]
                text=next((t for t in messages if 'Emotions:' in t),'')
                assert asyncio.get_running_loop().time()<deadline,('status failed to dispatch during real work',messages)
                await asyncio.sleep(.02)
            assert not h.results,'ordinary task completed or was cancelled before provider gate released'
            assert ThoughtStore().generation()==generation
        finally:release.set()
        deadline=asyncio.get_running_loop().time()+5
        while not h.results or h.adapter._active_sessions:
            assert asyncio.get_running_loop().time()<deadline,'real ordinary task failed to finish'
            await asyncio.sleep(.02)
        assert len(h.results)==1
        assert h.results[0]['final_response']=='ack'
        assert not h.results[0].get('interrupted') and not h.results[0].get('failed')
    try:asyncio.run(run())
    finally:release.set()

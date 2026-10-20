"""Actual Telegram and desktop command dispatch with synthetic authenticated owners."""
import asyncio
import json
import time
import yaml
import pytest
from agent import auxiliary_client as aux
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def inbound_command(h, text, mid=101):
    original=h.adapter.send.side_effect
    async def run():
        completed=asyncio.Event()
        async def sent(*a,**kw):
            completed.set()
            if original is not None:
                result=original(*a,**kw)
                if hasattr(result,'__await__'):
                    return await result
            return h.adapter.send.return_value
        h.adapter.send.side_effect=sent
        before=h.adapter.send.await_count
        await h.adapter._handle_text_message(h.update(text,message_id=mid),object())
        await asyncio.wait_for(completed.wait(),5)
        assert h.adapter.send.await_count==before+1
        deadline=asyncio.get_running_loop().time()+5
        while h.adapter._active_sessions:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        return h.adapter.send.await_args
    try:
        args=asyncio.run(run())
    finally:
        h.adapter.send.side_effect=original
    return args.args[1] if len(args.args)>1 else args.kwargs['content']


def test_registry_and_actual_telegram_disabled_command_has_no_inference_or_db(monkeypatch,tmp_path):
    from clover_cli.commands import COMMANDS, GATEWAY_KNOWN_COMMANDS, telegram_bot_commands
    assert '/emotions' in COMMANDS and 'emotions' in GATEWAY_KNOWN_COMMANDS
    assert any(row[0]=='emotions' for row in telegram_bot_commands())
    h,requests,bot=setup_host(monkeypatch,tmp_path)
    try:
        config=yaml.safe_load((h.home/'config.yaml').read_text()); config['presence']['enabled']=False
        (h.home/'config.yaml').write_text(yaml.safe_dump(config))
        from cron.presence import ThoughtStore
        assert not ThoughtStore().path.exists()
        before=h.db.get_session_count() if hasattr(h.db,'get_session_count') else None
        text=inbound_command(h,'/emotions')
        assert 'Emotions: disabled' in text and 'unknown' in text and 'Initiative:' in text
        print('EXECUTED_SYNTHETIC_EMOTIONS_OFF_START\n'+text+'\nEXECUTED_SYNTHETIC_EMOTIONS_OFF_END')
        assert 'openai' in text and not requests and bot.send_message.await_count==0
        assert not ThoughtStore().path.exists()
        assert h.results==[]
    finally:h.close()


def test_actual_telegram_status_reads_recent_owner_not_raw_evidence(monkeypatch,tmp_path):
    h,requests,bot=setup_host(monkeypatch,tmp_path)
    try:
        h.inbound_human('The previous attempt failed and I am frustrated. Private synthetic detail.',message_id=100)
        count=len(requests)
        from agent.emotions import _owners
        from agent.presence_runtime import clock
        before=[(str(k),o._latest,o._active()) for k,(o,e) in _owners.items()]
        text=inbound_command(h,'/emotions')
        assert 'careful_brief' in text and 'confidence' in text and 'expires' in text, (text,before,repr(_owners))
        assert 'Private synthetic detail' not in text
        print('EXECUTED_SYNTHETIC_EMOTIONS_RECENT_START\n'+text+'\nEXECUTED_SYNTHETIC_EMOTIONS_RECENT_END')
        assert len(requests)==count and len(h.results)==1
    finally:h.close()


def test_desktop_command_dispatch_checks_owning_transport(monkeypatch,tmp_path):
    from tests.tui_gateway.test_presence_integration import desktop_host, DesktopTransport
    from tui_gateway import server
    h,requests,_=setup_host(monkeypatch,tmp_path)
    agent,transport,sid,session,send=desktop_host(h,monkeypatch)
    try:
        send('The previous attempt failed and I am frustrated. Please be brief.')
        count=len(requests)
        from agent.emotions import _owners
        assert count==1 and any(owner._latest for owner,expiry in _owners.values()), (count,repr(_owners))
        result=server.dispatch({'jsonrpc':'2.0','id':'status','method':'command.dispatch',
            'params':{'session_id':sid,'name':'emotions'}},transport=transport)
        assert 'careful_brief' in result['result']['output'],(result,repr(_owners))
        stolen=server.dispatch({'jsonrpc':'2.0','id':'stolen','method':'command.dispatch',
            'params':{'session_id':sid,'name':'emotions'}},transport=DesktopTransport())
        assert 'careful_brief' not in json.dumps(stolen)
        assert len(requests)==count
    finally:h.close()


def test_unminted_internal_or_shared_commands_never_disclose(monkeypatch,tmp_path):
    from types import SimpleNamespace
    h,requests,_=setup_host(monkeypatch,tmp_path)
    try:
        source=SimpleNamespace(platform=SimpleNamespace(value='telegram'),chat_type='dm',chat_id='12345',user_id='12345')
        monkeypatch.setattr(h.runner,'_is_user_authorized',lambda source:True)
        event=SimpleNamespace(source=source,internal=False,get_command_args=lambda:'')
        text=asyncio.run(h.runner._handle_emotions_command(event))
        assert 'authenticated private' in text and 'Provider route:' not in text
        event.internal=True
        assert 'Provider route:' not in asyncio.run(h.runner._handle_emotions_command(event))
        event.internal=False;source.chat_type='group'
        assert 'Provider route:' not in asyncio.run(h.runner._handle_emotions_command(event))
        assert not requests
    finally:h.close()


def test_status_owner_purges_expired_and_separates_exact_scope(monkeypatch,tmp_path):
    from datetime import timedelta
    from dataclasses import replace
    from agent.emotions import _owners,render
    from agent import presence_runtime
    h,requests,_=setup_host(monkeypatch,tmp_path)
    try:
        h.inbound_human('I am frustrated. Synthetic private content.',message_id=100)
        key=next(k for k in _owners if k.scope.profile==str(h.home.resolve()))
        assert 'careful_brief' in render(key=key)
        for foreign in [replace(key,conversation_root='foreign'),replace(key,scope=replace(key.scope,project='/foreign')),
                        replace(key,scope=replace(key.scope,profile='/foreign')),replace(key,scope=replace(key.scope,root='foreign'))]:
            assert 'careful_brief' not in render(key=foreign)
        now=presence_runtime.clock()
        monkeypatch.setattr(presence_runtime,'clock',lambda:now+timedelta(seconds=121))
        text=render(key=key)
        assert 'unknown (expired)' in text and 'confidence=' not in text and key not in _owners
        assert len(requests)==1
    finally:h.close()


def test_desktop_emotions_cannot_be_shadowed_by_an_exec_quick_command(monkeypatch,tmp_path):
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    import subprocess
    h,requests,_=setup_host(monkeypatch,tmp_path)
    agent,transport,sid,session,send=desktop_host(h,monkeypatch)
    try:
        config=yaml.safe_load((h.home/'config.yaml').read_text())
        config['quick_commands']={'emotions':{'type':'exec','command':'do-not-execute'}}
        (h.home/'config.yaml').write_text(yaml.safe_dump(config))
        monkeypatch.setattr(subprocess,'run',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('status executed shell')))
        reply=server.dispatch({'jsonrpc':'2.0','id':'status','method':'command.dispatch',
            'params':{'session_id':sid,'name':'emotions'}},transport=transport)
        assert 'Emotions:' in reply['result']['output'],reply
        assert not requests
    finally:h.close()


def test_real_cli_process_command_and_desktop_slash_exec_are_read_only(monkeypatch,tmp_path):
    import io
    from rich.console import Console
    from cli import CloverCLI
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    h,requests,_=setup_host(monkeypatch,tmp_path)
    agent,transport,sid,session,send=desktop_host(h,monkeypatch)
    try:
        config=yaml.safe_load((h.home/'config.yaml').read_text());config['presence']['enabled']=False
        (h.home/'config.yaml').write_text(yaml.safe_dump(config))
        output=io.StringIO();cli=CloverCLI.__new__(CloverCLI);cli.console=Console(file=output,width=120)
        assert cli.process_command('/emotions') is True
        assert 'Emotions: disabled' in output.getvalue()
        import threading
        completed=threading.Event()
        write=transport.write
        def capture(frame):
            result=write(frame)
            if frame.get('id')=='slash': completed.set()
            return result
        transport.write=capture
        result=server.dispatch({'jsonrpc':'2.0','id':'slash','method':'slash.exec',
            'params':{'session_id':sid,'command':'/emotions'}},transport=transport)
        if result is None:
            assert completed.wait(5),transport.frames
            result=next(frame for frame in transport.frames if frame.get('id')=='slash')
        assert 'Emotions: disabled' in result['result']['output'],result
        assert result['result']['type']=='exec' and not requests
        from cron.presence import ThoughtStore
        assert not ThoughtStore().path.exists()
    finally:h.close()


@pytest.mark.parametrize('race',['principal','profile','project','close'])
def test_desktop_status_rechecks_owner_after_readback(monkeypatch,tmp_path,race):
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    from agent import emotions
    h,requests,_=setup_host(monkeypatch,tmp_path)
    agent,transport,sid,session,send=desktop_host(h,monkeypatch)
    real=emotions.render
    def revoke(**kwargs):
        text=real(**kwargs)
        if race=='principal':transport.auth_identity={'provider':'synthetic-login','user_id':'foreign-owner'}
        elif race=='profile':session['profile_home']=str(tmp_path/'foreign-profile')
        elif race=='project':session['cwd']=str(tmp_path/'foreign-project')
        else:transport.closed=True
        return text
    monkeypatch.setattr(emotions,'render',revoke)
    try:
        reply=server.dispatch({'jsonrpc':'2.0','id':'status','method':'command.dispatch',
            'params':{'session_id':sid,'name':'emotions'}},transport=transport)
        assert 'Provider route:' not in json.dumps(reply),reply
        assert not requests
    finally:h.close()

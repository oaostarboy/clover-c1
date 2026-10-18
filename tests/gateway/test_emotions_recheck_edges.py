"""Independent command-edge and cold-config probes with synthetic data only."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from tests.gateway.test_emotions_independent_review import host, seed, capture_command  # noqa: F401


@pytest.mark.parametrize('target,field,value', [
    ('event','message_id','changed'), ('event','platform_update_id','changed'),
    ('event','allow_gateway_control',False), ('event','internal',True),
    ('source','user_id_alt','other'), ('source','chat_id_alt','other'),
    ('source','scope_id','other'), ('source','parent_chat_id','other'),
    ('source','guild_id','other'), ('source','is_bot',True),
    ('source','role_authorized',True), ('source','profile_route_rejected',True),
])
def test_capability_extra_original_fields_are_immutable(host, monkeypatch, target, field, value):
    event = capture_command(host, monkeypatch)
    setattr(event if target == 'event' else event.source, field, value)
    text = asyncio.run(host.runner._handle_emotions_command(event))
    assert 'Provider route:' not in text
    assert not host.requests


def test_duplicate_update_id_is_not_a_new_delivery(host, monkeypatch):
    seed(host)
    first = capture_command(host, monkeypatch)
    captured = []
    with monkeypatch.context() as mp:
        mp.setattr(host.adapter, '_enqueue_text_event', captured.append)
        update = host.update('/emotions', message_id=102)
        update.update_id = 101  # Same transport update, rebuilt with other message.
        asyncio.run(host.adapter._handle_text_message(update, object()))
    assert 'confidence=0.90' in asyncio.run(host.runner._handle_emotions_command(first))
    text = asyncio.run(host.runner._handle_emotions_command(captured[0]))
    assert 'Provider route:' not in text, text


def test_replay_guard_capacity_holds_and_expiry_is_bounded(host, monkeypatch):
    from agent import workstream_origin as origin
    monkeypatch.setattr(origin, '_COMMAND_DELIVERIES', {(i,): float('inf') for i in range(4096)})
    event = capture_command(host, monkeypatch)
    assert 'Provider route:' not in asyncio.run(host.runner._handle_emotions_command(event))
    monkeypatch.setattr(origin, '_COMMAND_DELIVERIES', {(i,): -1 for i in range(4096)})
    captured = []
    with monkeypatch.context() as mp:
        mp.setattr(host.adapter, '_enqueue_text_event', captured.append)
        asyncio.run(host.adapter._handle_text_message(host.update('/emotions', message_id=103), object()))
    assert len(origin._COMMAND_DELIVERIES) == 1
    assert 'Provider route:' in asyncio.run(host.runner._handle_emotions_command(captured[0]))


@pytest.mark.parametrize('managed,user,expected', [
    ('', None, 'disabled'),
    ('presence: [', 'presence:\n  enabled: true\n', 'unavailable'),
    ('[not,a,mapping]', 'presence:\n  enabled: true\n', 'unavailable'),
    ('presence:\n  enabled: false\n', 'presence:\n  enabled: true\n', 'disabled'),
    ('presence:\n  enabled: true\n  budget_mode: unknown\n', '', 'held'),
    ('', 'presence:\n  enabled: true\nmodel:\n  provider: openai\n  default: ${SYNTHETIC_API_KEY}\n', 'unavailable'),
    ('', 'presence: &cycle\n  enabled: true\n  budget_mode: *cycle\n', 'unavailable'),
])
def test_new_cold_managed_inventory_before_import(tmp_path, managed, user, expected):
    home = tmp_path/'home'; home.mkdir()
    profile = home/'profile'
    policy = home/'managed'; policy.mkdir()
    (policy/'config.yaml').write_text(managed)
    if user is not None:
        profile.mkdir()
        (profile/'config.yaml').write_text(user)
        (profile/'.env').write_text('SYNTHETIC_API_KEY=not-to-be-read\n')
        (profile/'auth.json').write_text('{"synthetic":"not-to-be-read"}')
    source = Path(__file__).resolve().parents[2]
    code = '''import sys,pathlib,json,os,socket
home=pathlib.Path.home()
def inventory():
 return {str(p.relative_to(home)):p.read_bytes().hex() if p.is_file() else '<directory>' for p in home.rglob('*')}
before=inventory()
blocked=[]
armed=True
def audit(event,args):
 if not armed:return
 if event=='open' and isinstance(args[0],str):
  name=pathlib.Path(args[0]).name
  mode=args[1];flags=args[2]
  if name in {'.env','auth.json'} or (isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)):
   blocked.append([event,str(args[0])]);raise AssertionError('forbidden open')
 if event in {'os.mkdir','os.remove','os.rename','os.rmdir','os.symlink','os.link'}:
  blocked.append([event,str(args)]);raise AssertionError('forbidden mutation')
sys.addaudithook(audit)
real_getitem=os._Environ.__getitem__
def getitem(self,key):
 if key=='SYNTHETIC_API_KEY':
  blocked.append(['env',key]);raise AssertionError('credential env read')
 return real_getitem(self,key)
os._Environ.__getitem__=getitem
class Guard:
 def find_spec(self,fullname,path=None,target=None):
  if fullname in {'openai','anthropic','clover_cli.config','clover_cli.auth','clover_cli.auth_pool','agent.auxiliary_client','agent.presence_runtime','cron.presence'}:
   blocked.append(['import',fullname]);raise AssertionError('runtime import')
sys.meta_path.insert(0,Guard())
def deny(*a,**k):
 blocked.append(['network','attempt']);raise AssertionError('network forbidden')
socket.socket.connect=deny;socket.getaddrinfo=deny
from agent.emotions import render
text=render()
armed=False
print(json.dumps(dict(before=before,after=inventory(),text=text,blocked=blocked,modules=sorted(sys.modules))))
'''
    result = subprocess.run([sys.executable,'-B','-c',code],cwd=source,env={
        'PATH':os.environ['PATH'],'HOME':str(home),'CLOVER_HOME':str(profile),
        'CLOVER_MANAGED_DIR':str(policy),'PYTHONPATH':str(source),
        'SYNTHETIC_API_KEY':'not-to-be-read'},capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    print('INDEPENDENT_COLD_INVENTORY',json.dumps(data))
    assert data['before'] == data['after']
    assert not data['blocked'], data['blocked']
    assert expected in data['text'], data['text']
    assert 'confidence=' not in data['text']

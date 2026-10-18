"""Cold-process /emotions inventory; all files/configuration are synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize('config', [None, 'presence:\n  enabled: false\n',
    'presence:\n  enabled: true\n', 'presence: [',
    'presence:\n  enabled: false\nmodel:\n  provider: openai\n  default: synthetic-model\n'])
def test_true_cold_status_no_writes_or_runtime_imports(tmp_path, config):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.env').write_text('SYNTHETIC_SECRET=do-not-read\n')
    (home / 'auth.json').write_text('{"synthetic":"do-not-read"}')
    if config is not None:
        (home / 'config.yaml').write_text(config)
    source = Path(__file__).resolve().parents[2]
    code = '''import pathlib,socket,sys,json
home=pathlib.Path.home()
def inventory():
 return {str(p.relative_to(home)):p.read_bytes().hex() if p.is_file() else '<directory>' for p in home.rglob('*')}
before=inventory()
armed=True
secret_reads=[]
def audit(event,args):
 if armed and event=='open' and isinstance(args[0],str) and pathlib.Path(args[0]).name in {'.env','auth.json'}:
  secret_reads.append(args[0]);raise AssertionError('secret store access forbidden')
sys.addaudithook(audit)
attempts=[]
def deny(*a,**k):
 attempts.append(True);raise AssertionError('network forbidden')
socket.socket.connect=deny;socket.getaddrinfo=deny
modules=set(sys.modules)
from agent.emotions import render
text=render()
armed=False
print(json.dumps(dict(before=before,after=inventory(),text=text,attempts=attempts,secret_reads=secret_reads,modules=sorted(set(sys.modules)-modules))))
'''
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=source,
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'), 'HOME':str(home),
             'CLOVER_HOME':str(home), 'PYTHONPATH':str(source)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['before'] == data['after'], data
    assert not data['attempts'] and not data['secret_reads']
    forbidden = {'agent.auxiliary_client', 'clover_cli.auth', 'clover_cli.auth_pool', 'clover_cli.config'}
    if config != 'presence:\n  enabled: true\n':
        forbidden |= {'agent.presence_runtime', 'cron.presence'}
    assert not forbidden.intersection(data['modules']), data['modules']
    assert 'confidence=' not in data['text']
    print(json.dumps(data))


@pytest.mark.parametrize('model,root_alias,expected', [
    ({'name':'synthetic-alias', 'provider':'openai'}, {}, ('openai', 'synthetic-alias')),
    ({'default':{'provider':'anthropic','model':'synthetic-nested'},'provider':'openai'}, {}, ('openai','synthetic-nested')),
    ({'default':'synthetic-root'}, {'provider':'anthropic'}, ('anthropic','synthetic-root')),
    ({'default':'synthetic-explicit','provider':'openai','name':'unused'}, {}, ('openai','synthetic-explicit')),
])
def test_snapshot_legacy_route_aliases_do_not_migrate_files(monkeypatch, tmp_path, model, root_alias, expected):
    import yaml
    from agent.emotions_config import snapshot
    from agent.bounded_completion import select_route
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    data = {'model':model, **root_alias}
    path = tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump(data))
    before = path.read_bytes()
    files_before = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')}
    route = select_route(snapshot())
    assert (route.provider, route.model) == expected
    assert path.read_bytes() == before
    assert {str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')} == files_before


@pytest.mark.parametrize('control', ['stopped', 'daily_request_quota_exhausted'])
def test_cold_durable_profile_hold_no_config_or_secret_runtime(monkeypatch, tmp_path, control):
    from cron.presence import ThoughtStore
    from datetime import datetime, timezone
    import yaml
    home = tmp_path/'cold-durable'
    home.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(home))
    (home/'config.yaml').write_text(yaml.safe_dump({'presence':{'enabled':True},
        'proactive':{'enabled':True, 'mode':'active', 'suggestions':{'enabled':True}}}))
    store = ThoughtStore()
    if control == 'stopped':
        store.stop()
    else:
        for i in range(3):
            assert store.reserve_assessment(event_id=f'synthetic-{i}', thought_id=f'synthetic-{i}',
                revision=1, day=datetime.now(timezone.utc).date().isoformat(), max_calls=3, tokens=1)
    code = '''import sys,json,pathlib,socket
home=pathlib.Path.home()
def inventory():
 return {str(p.relative_to(home)):p.read_bytes().hex() for p in home.rglob('*') if p.is_file()}
before=inventory()
def deny(*a,**k): raise AssertionError('network forbidden')
socket.socket.connect=deny;socket.getaddrinfo=deny
from agent.emotions import render
text=render()
print(json.dumps(dict(before=before,after=inventory(),text=text,modules=sorted(sys.modules))))
'''
    source = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=source,
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'), 'HOME':str(home),
             'CLOVER_HOME':str(home), 'PYTHONPATH':str(source)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['before'] == data['after']
    assert f'reason: {control}' in data['text']
    assert not {'cron', 'cron.presence', 'clover_cli.config', 'agent.presence_runtime',
                'clover_cli.auth', 'agent.auxiliary_client'}.intersection(data['modules'])

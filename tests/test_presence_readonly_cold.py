"""Cold process OS audit boundary: no hidden init, auth/HTTP before admission."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.clover_cli.test_runtime_provider_readonly import profile, inventory

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX permission and symlink guarantees')
ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import json, os, sys
from pathlib import Path
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
home = Path(os.environ['CLOVER_HOME'])
guard = ReadOnlyEvaluationGuard(home=home, source=Path.cwd(), write_dirs=[])
guard.install()
error = None
try:
    from clover_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(read_only=True)
    selected = runtime['credential_pool'].current()
    assert selected is not None
    status = 'selected'
except Exception as exc:
    status, error = 'held', type(exc).__name__
print(json.dumps({'status':status,'error':error, 'guard':guard.receipt()}))
'''


def cold(monkeypatch, tmp_path, mutate=None):
    home, _, _ = profile(monkeypatch, tmp_path)
    user_home = tmp_path / 'user'
    user_home.mkdir(mode=0o700)
    if mutate:
        mutate(home)
    before = inventory(home) if home.exists() and not home.is_symlink() else None
    env = {k:v for k,v in os.environ.items() if k in ('PATH','LANG','TZ')}
    env.update(HOME=str(user_home), CLOVER_HOME=str(home), CLOVER_MANAGED_DIR=str(tmp_path/'managed'),
               PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
    run = subprocess.run([sys.executable, '-c', PROBE], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    if before is not None:
        assert inventory(home) == before
    assert result['guard']['denied_writes'] == []
    assert result['guard']['network_events'] == []
    assert result['guard']['denied_private_reads'] == []
    return result


def test_cold_selected_path_has_no_hidden_initialization(monkeypatch, tmp_path):
    result = cold(monkeypatch, tmp_path)
    assert result['status'] == 'selected'
    assert result['guard']['baseline_unchanged'] is True


@pytest.mark.parametrize('catalog_cache', [False, True])
def test_cold_full_evaluator_keeps_os_guard_through_actual_wire(monkeypatch, tmp_path, catalog_cache):
    home, config, _ = profile(monkeypatch, tmp_path)
    import yaml
    config['presence'] = {'enabled': False}
    config['proactive'] = {'aux': {'max_calls_per_event': 0, 'max_calls_per_day': 0, 'max_output_tokens': 0}}
    (home/'config.yaml').write_text(yaml.safe_dump(config))
    if catalog_cache:
        (home/'models_dev_cache.json').write_text('SCRIPTED-corrupt-public-catalog-do-not-repair')
    quota = tmp_path / 'quota'
    quota.mkdir(mode=0o700)
    code = r'''
import json, os
from pathlib import Path
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
quota = Path(os.environ['QUOTA'])
guard = ReadOnlyEvaluationGuard(home=Path(os.environ['CLOVER_HOME']), source=Path.cwd(), write_dirs=[quota])
# Diagnostic only: preserve the original denial and record OS-backed fd facts.
import fcntl, sys
original_audit = guard.audit
denied_operations = []
def diagnosed_audit(event, args):
    try:
        return original_audit(event, args)
    except PermissionError:
        if event == 'open':
            row = {'event': event, 'args': list(args), 'stack': []}
            frame = sys._getframe(1)
            while frame is not None:
                row['stack'].append([frame.f_code.co_filename, frame.f_code.co_name, frame.f_lineno])
                frame = frame.f_back
            if isinstance(args[0], int):
                fd = args[0]
                info = os.fstat(fd)
                row.update(fd_flags=fcntl.fcntl(fd, fcntl.F_GETFL),
                           fd_access=fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE,
                           fd_mode=info.st_mode, fd_device=info.st_dev, fd_inode=info.st_ino,
                           fd_uid=info.st_uid, fd_gid=info.st_gid, fd_rdev=info.st_rdev)
                sys.stderr.write('COLD_AUDIT_BEFORE_GETPATH ' + json.dumps(row) + '\n')
                if sys.platform == 'darwin':
                    try:
                        path = fcntl.fcntl(fd, 50, bytes(1024))  # Darwin F_GETPATH.
                        row.update(f_getpath_errno=0, fd_target=os.fsdecode(path.split(b'\0', 1)[0]))
                    except OSError as exc:
                        row.update(f_getpath_errno=exc.errno, fd_target=None)
                else:
                    row['fd_target'] = os.readlink('/proc/self/fd/' + str(fd))
            denied_operations.append(row)
            sys.stderr.write('COLD_AUDIT_DENIED ' + json.dumps(row) + '\n')
        raise
sys.addaudithook(diagnosed_audit)
from scripts.eval_presence_current_turn import execute_isolated_cases, load_cases
from agent import auxiliary_client as aux
import httpx
wires = []
def respond(request):
    assert json.loads((quota/'presence-eval-quota.json').read_text())['used'] == len(wires)+1
    wires.append(request.url.host)
    event = {'type':'response.completed','response':{'output':[{'type':'message','content':[{'type':'output_text','text':'{"affect":null,"thought":null}'}]}]}}
    return httpx.Response(200,headers={'Content-Type':'text/event-stream'},content='data: '+json.dumps(event)+'\n\n')
httpx.HTTPTransport.handle_request = lambda transport, request: respond(request)
async def scripted_async(transport, request):
    return respond(request)
httpx.AsyncHTTPTransport.handle_async_request = scripted_async
cases,_ = load_cases('normative')
result,attempts,invocations = execute_isolated_cases([next(c for c in cases if c['id']=='thought_user_interest_en')],max_calls=3,quota_dir=quota,request_budget_consent=True)
print(json.dumps({'status':result[0]['status'],'wires':wires,'attempts':attempts,'invocations':invocations,'guard':guard.receipt(),'denied_operations':denied_operations}))
'''
    env = {k:v for k,v in os.environ.items() if k in ('PATH','LANG','TZ')}
    env.update(HOME=str(tmp_path), CLOVER_HOME=str(home), CLOVER_MANAGED_DIR=str(tmp_path/'managed'),
               QUOTA=str(quota), PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
    run = subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,capture_output=True,text=True,timeout=20)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result['guard']['denied_writes'] == [], json.dumps(result, sort_keys=True)
    assert result['guard']['denied_private_reads'] == []
    assert result['guard']['network_events'] == []
    assert result['status'] == 'measured' and result['wires'] == ['chatgpt.com']
    assert result['attempts'] == result['invocations'] == 1


@pytest.mark.parametrize('bad', ['missing_config','missing_home','ancestor_symlink','invalid_yaml','insecure_config','insecure_home','config_symlink','auth_symlink','expired','multiple','missing_auth'])
def test_cold_invalid_state_fails_closed_without_auth_or_network_repair(monkeypatch, tmp_path, bad):
    def mutate(home):
        config, auth = home/'config.yaml', home/'auth.json'
        if bad == 'missing_home':
            config.unlink(); auth.unlink(); home.rmdir()
        elif bad == 'ancestor_symlink':
            real = home.parent/'real-profile'
            home.rename(real)
            home.symlink_to(real, target_is_directory=True)
        elif bad == 'missing_config': config.unlink()
        elif bad == 'invalid_yaml': config.write_text('security: [invalid')
        elif bad == 'insecure_config': config.chmod(0o666)
        elif bad == 'insecure_home': home.chmod(0o755)
        elif bad.endswith('_symlink'):
            p = config if bad == 'config_symlink' else auth
            target = home / 'link-target'
            p.rename(target)
            p.symlink_to(target)
        elif bad == 'missing_auth': auth.unlink()
        else:
            store = json.loads(auth.read_text())
            rows = store['credential_pool']['openai-codex']
            if bad == 'multiple': rows.append(dict(rows[0], id='SCRIPTED-other'))
            else:
                from tests.agent.test_provider_security_corrected import entry
                rows[0] = entry('SCRIPTED-expired', expired=True).to_dict()
            auth.write_text(json.dumps(store))
    result = cold(monkeypatch, tmp_path, mutate)
    assert result['status'] == 'held'
    if bad in {'missing_config','missing_home','ancestor_symlink','invalid_yaml','insecure_config','insecure_home','config_symlink'}:
        assert result['guard']['auth_reads'] == 0


def test_guard_denies_home_chmod_and_network_before_syscalls(monkeypatch, tmp_path):
    home, _, _ = profile(monkeypatch, tmp_path)
    before = inventory(home), home.stat().st_mode
    code = r'''
import json, os, socket
from pathlib import Path
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
home = Path(os.environ['CLOVER_HOME'])
guard = ReadOnlyEvaluationGuard(home=home, source=Path.cwd(), write_dirs=[])
guard.install()
blocked = 0
for operation in (lambda: os.chmod(home,0o777), lambda: (home/'forbidden').write_text('no'), lambda: socket.getaddrinfo('example.invalid',443)):
    try: operation()
    except PermissionError: blocked += 1
print(json.dumps({'blocked':blocked,'receipt':guard.receipt()}))
'''
    env = {'HOME':str(tmp_path), 'CLOVER_HOME':str(home), 'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONPATH':str(ROOT)}
    run = subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,capture_output=True,text=True,timeout=15)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result['blocked'] == 3 and result['receipt']['baseline_unchanged']
    assert len(result['receipt']['denied_writes']) == 2
    assert result['receipt']['network_events'][0]['admitted'] is False
    assert (inventory(home), home.stat().st_mode) == before

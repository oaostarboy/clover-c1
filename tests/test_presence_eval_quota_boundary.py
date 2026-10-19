"""Real quota I/O in disposable subprocesses; no provider or profile selection."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX isolated quota boundary')

PROBE = r'''
import json, os, sys
from pathlib import Path
from scripts.eval_presence_current_turn import _reserve_isolated_call
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
home, quota = map(Path, sys.argv[1:3])
action = sys.argv[3]
private = home/'private.json'
private.write_text(json.dumps({'version':1, 'binding':'SCRIPTED', 'used':0}))
private.chmod(0o600)
ledger = quota/'presence-eval-quota.json'
if action in ('hardlink', 'hardlink_fd'): os.link(private, ledger)
elif action in ('symlink', 'symlink_string'): ledger.symlink_to(private)
elif action == 'dotdot': quota = quota/'..'/'home'
fd = os.open(ledger, os.O_RDWR) if action == 'hardlink_fd' else None
g = ReadOnlyEvaluationGuard(home=home, source=Path.cwd(), write_dirs=[Path(sys.argv[2])])
g.install()
status, reserved = 'allowed', []
try:
    if action in ('symlink_open', 'symlink_string'):
        # Neither traversal nor a leaf alias may truncate the private target.
        target = ledger if action == 'symlink_string' else quota/'..'/'home'/'private.json'
        with open(target, 'w') as stream: stream.write('changed')
    elif action == 'hardlink_fd':
        with os.fdopen(fd,'r+') as stream: stream.write('changed')
        fd = None
    else:
        reserved = [_reserve_isolated_call(quota, 'SCRIPTED', 3) for _ in range(4)]
except (OSError, ValueError): status = 'held'
finally:
    if fd is not None:
        try: os.close(fd)
        except OSError: pass
print(json.dumps({'status':status, 'reserved':reserved, 'guard':g.receipt()}))
'''


def probe(tmp_path, action, code=PROBE):
    home, quota = tmp_path/'home', tmp_path/'quota'
    home.mkdir(mode=0o700); quota.mkdir(mode=0o700)
    env = {k:v for k,v in os.environ.items() if k in ('PATH','LANG','TZ')}
    env.update(HOME=str(tmp_path), CLOVER_HOME=str(home), PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
    run = subprocess.run([sys.executable, '-B', '-c', code, str(home), str(quota), action],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout), home, quota


@pytest.mark.parametrize('action', ['ordinary','hardlink','hardlink_fd','symlink','symlink_string','dotdot','symlink_open'])
def test_guarded_reservation_preserves_private_files(tmp_path, action):
    result, home, quota = probe(tmp_path, action)
    assert json.loads((home/'private.json').read_text()) == {'version':1,'binding':'SCRIPTED','used':0}
    assert result['guard']['baseline_unchanged'] is True
    assert result['guard']['network_events'] == []
    assert result['status'] == ('allowed' if action == 'ordinary' else 'held'), result
    assert result['reserved'] == ([True,True,True,False] if action == 'ordinary' else [])
    if action == 'ordinary':
        assert json.loads((quota/'presence-eval-quota.json').read_text())['used'] == 3


@pytest.mark.parametrize('action', ['ordinary','hardlink','symlink','dotdot'])
def test_reservation_itself_rejects_aliases_without_relying_on_audit(tmp_path, action):
    code = PROBE.replace('g.install()', '')
    result, home, quota = probe(tmp_path, action, code)
    assert json.loads((home/'private.json').read_text())['used'] == 0
    assert result['status'] == ('allowed' if action == 'ordinary' else 'held')
    assert result['reserved'] == ([True,True,True,False] if action == 'ordinary' else [])

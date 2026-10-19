"""OS-backed descriptor controls for the isolated evaluation audit guard."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor audit boundary')

PROBE = r'''
import fcntl, json, os, sys
from pathlib import Path
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
home, quota, target = map(Path, sys.argv[1:4])
action = sys.argv[4]
writing = action.endswith('write')
fd = -1 if action.startswith('unknown') else os.open(target, os.O_RDWR if writing else os.O_RDONLY)
access = None if fd == -1 else fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
guard = ReadOnlyEvaluationGuard(home=home, source=Path.cwd(), write_dirs=[quota])
guard.install()
status, value = 'allowed', None
try:
    if fd == -1:
        # Exercise failed target resolution without invoking an invalid OS open.
        guard.audit('open', (fd, 'r+' if writing else 'r', os.O_RDWR if writing else os.O_RDONLY))
    else:
        with os.fdopen(fd, 'r+' if writing else 'r') as stream:
            fd = None
            if writing:
                stream.write('changed')
            else:
                value = stream.read()
except PermissionError:
    status = 'denied'
finally:
    if fd is not None and fd != -1:
        os.close(fd)
print(json.dumps(dict(status=status, value=value, access=access, guard=guard.receipt())))
'''


@pytest.mark.parametrize('action,expected', [
    ('quota_read', 'allowed'),
    ('quota_write', 'allowed'),
    ('private_read', 'denied'),
    ('outside_write', 'denied'),
    ('unknown_read', 'denied'),
    ('unknown_write', 'denied'),
])
def test_descriptor_targets_preserve_allow_and_deny_boundaries(tmp_path, action, expected):
    home, quota = tmp_path/'home', tmp_path/'quota'
    home.mkdir(mode=0o700)
    quota.mkdir(mode=0o700)
    target = (quota/'charge.json' if action.startswith('quota') else
              home/'private.txt' if action == 'private_read' else tmp_path/'outside.txt')
    target.write_text('original')
    target.chmod(0o600)
    env = {k:v for k,v in os.environ.items() if k in ('PATH','LANG','TZ')}
    env.update(HOME=str(tmp_path), CLOVER_HOME=str(home), PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
    run = subprocess.run([sys.executable, '-c', PROBE, str(home), str(quota), str(target), action],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result['status'] == expected, result
    assert result['guard']['network_events'] == []
    assert result['guard']['baseline_unchanged'] is True
    assert target.read_text() == ('changedl' if action == 'quota_write' else 'original')
    if expected == 'denied':
        denied = result['guard']['denied_writes'] if action.endswith('write') else result['guard']['denied_private_reads']
        assert len(denied) == 1, result
    else:
        assert result['guard']['denied_writes'] == result['guard']['denied_private_reads'] == []
        assert result['access'] == (os.O_RDWR if action.endswith('write') else os.O_RDONLY)
        if action == 'quota_read':
            assert result['value'] == 'original'

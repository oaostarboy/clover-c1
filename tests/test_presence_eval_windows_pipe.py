"""Real Windows descriptor rights for non-filesystem read-only pipe admission."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows pipe handle rights')


def child(tmp_path, body):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / 'home'
    home.mkdir()
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT') if k in os.environ}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), CLOVER_HOME=str(home),
               PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE='1',
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    code = '''
import json, os, sys, subprocess
from pathlib import Path
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
home=Path(os.environ['CLOVER_HOME'])
guard=ReadOnlyEvaluationGuard(home=home,source=Path.cwd(),write_dirs=[])
''' + body
    p = subprocess.run([sys.executable, '-c', code], cwd=root, env=env,
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_actual_readonly_pipe_can_be_wrapped_under_guard(tmp_path):
    result = child(tmp_path, '''
r,w=os.pipe()
os.write(w,b'ok');os.close(w)
guard.install()
with os.fdopen(r,'rb') as stream:
    assert stream.read()==b'ok'
assert guard.denied_private_reads==[]
print(json.dumps({'readonly_pipe':True,'refusals':guard.denied_private_reads}))
''')
    assert result == {'readonly_pipe': True, 'refusals': []}


def test_actual_writable_pipe_cannot_be_wrapped_as_readonly(tmp_path):
    result = child(tmp_path, '''
r,w=os.pipe()
guard.install()
try:
    os.fdopen(w,'rb',closefd=False)
except PermissionError as exc:
    assert str(exc)=='evaluation_private_read_forbidden'
else:
    raise AssertionError('writable pipe granted')
os.close(r);os.close(w)
print(json.dumps({'denied':guard.denied_private_reads}))
''')
    assert result['denied'] == ['unresolved_descriptor']


def test_actual_preopened_regular_file_stays_unresolved_and_denied(tmp_path):
    result = child(tmp_path, '''
p=home/'private.json';p.write_bytes(b'private synthetic fixture')
fd=os.open(p,os.O_RDONLY)
guard.install()
try:
    os.fdopen(fd,'rb',closefd=False)
except PermissionError as exc:
    assert str(exc)=='evaluation_private_read_forbidden'
else:
    raise AssertionError('unresolved regular file granted')
os.close(fd)
print(json.dumps({'denied':guard.denied_private_reads}))
''')
    assert result['denied'] == ['unresolved_descriptor']


def test_actual_closed_descriptor_stays_denied(tmp_path):
    result = child(tmp_path, '''
r,w=os.pipe();os.close(r);os.close(w)
try:
    guard.audit('open',(r,'r',os.O_RDONLY))
except PermissionError as exc:
    assert str(exc)=='evaluation_private_read_forbidden'
else:
    raise AssertionError('closed descriptor granted')
print(json.dumps({'denied':guard.denied_private_reads}))
''')
    assert result['denied'] == ['unresolved_descriptor']


def test_actual_null_character_descriptor_is_not_a_pipe_grant(tmp_path):
    result = child(tmp_path, '''
fd=os.open('nul',os.O_RDONLY)
try:
    guard.audit('open',(fd,'r',os.O_RDONLY))
except PermissionError as exc:
    assert str(exc)=='evaluation_private_read_forbidden'
else:
    raise AssertionError('character descriptor granted')
os.close(fd)
print(json.dumps({'denied':guard.denied_private_reads}))
''')
    assert result['denied'] == ['unresolved_descriptor']


def test_pipe_construction_still_reaches_unchanged_subprocess_refusal(tmp_path):
    result = child(tmp_path, '''
guard.install()
try:
    subprocess.check_output([sys.executable,'-c','raise AssertionError("must not execute")'])
except PermissionError as exc:
    assert str(exc)=='evaluation_subprocess_forbidden',str(exc)
else:
    raise AssertionError('subprocess granted')
assert guard.denied_private_reads==[]
import socket
try:
    socket.getaddrinfo('example.invalid',443)
except PermissionError as exc:
    assert str(exc)=='evaluation_network_before_admission'
else:
    raise AssertionError('network granted')
print(json.dumps({'subprocess_denied':True,'network_denied':True,'refusals':guard.denied_private_reads}))
''')
    assert result == {'subprocess_denied': True, 'network_denied': True, 'refusals': []}

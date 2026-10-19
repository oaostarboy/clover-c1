"""Quota corruption and race controls use only fixture files and real OS calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_presence_eval_quota_boundary import ROOT, probe

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX isolated quota boundary')

STATE_PROBE = r'''
import json, os, sys
from pathlib import Path
from scripts.eval_presence_current_turn import _reserve_isolated_call
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard, evaluation_open
home, quota = map(Path, sys.argv[1:3]); action = sys.argv[3]
ledger = quota/'presence-eval-quota.json'
seed = b'{"version":1,"binding":"SCRIPTED","used":0}'
bad = {
    'empty': b'', 'malformed': b'{', 'array': b'[]', 'duplicate': b'{"version":1,"binding":"SCRIPTED","used":3,"used":0}',
    'bool_used': b'{"version":1,"binding":"SCRIPTED","used":true}',
    'bool_version': b'{"version":true,"binding":"SCRIPTED","used":0}',
    'wrong_binding': b'{"version":1,"binding":"OTHER","used":0}',
    'negative_used': b'{"version":1,"binding":"SCRIPTED","used":-1}',
    'over_used': b'{"version":1,"binding":"SCRIPTED","used":4}',
    'oversized': seed + b' '*4096, 'invalid_utf8': b'\xff',
    'short_read': seed + b' '*1024,
    'multibyte_oversized': seed + ('é'*2048).encode(),
}
if action in bad:
    ledger.write_bytes(bad[action]); ledger.chmod(0o600)
elif action == 'fifo': os.mkfifo(ledger, 0o600)
elif action == 'directory_leaf': ledger.mkdir(mode=0o700)
elif action == 'ledger_mode': ledger.write_bytes(seed); ledger.chmod(0o644)
elif action == 'exact_limit': ledger.write_bytes(seed+b' '*(4096-len(seed))); ledger.chmod(0o600)
elif action == 'root_mode': quota.chmod(0o755)
elif action == 'root_symlink':
    original = quota.parent/'real-quota'; quota.rename(original); quota.symlink_to(original, target_is_directory=True)
elif action == 'ancestor_symlink':
    original = quota.parent/'real-parent'; original.mkdir(mode=0o700)
    (original/'quota').mkdir(mode=0o700)
    alias = quota.parent/'alias'; alias.symlink_to(original, target_is_directory=True)
    quota = alias/'quota'
elif action == 'missing': quota.rmdir()
original = ledger.read_bytes().hex() if action in bad else None
reads=[]
real_read=os.read
def bounded(fd,n):
    reads.append(n)
    return real_read(fd,len(seed) if action == 'short_read' else n)
os.read=bounded
status, reservations = 'allowed', []
try:
    g=ReadOnlyEvaluationGuard(home=home,source=Path.cwd(),write_dirs=[quota]);g.install()
    if action == 'wrong_owner':
        # Real descriptor/stat data, with only the authorization principal
        # changed. No chown, elevated privilege or foreign live file needed.
        uid=os.geteuid();os.geteuid=lambda: uid+1
    if action == 'wrong_dirfd':
        directory=os.open(home,os.O_RDONLY|os.O_DIRECTORY)
        try: evaluation_open('outside-new.json',os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,dir_fd=directory)
        finally: os.close(directory)
    else:
        reservations=[_reserve_isolated_call(quota,'SCRIPTED',0 if action == 'zero' else 3)]
        if action == 'zero': reservations.append(_reserve_isolated_call(quota,'SCRIPTED',3))
except (OSError,ValueError): status='held'
print(json.dumps({'status':status,'reserved':reservations,'reads':reads,'original':original}))
'''


@pytest.mark.parametrize('action', [
    'empty','malformed','array','duplicate','bool_used','bool_version','wrong_binding',
    'negative_used','over_used','oversized','invalid_utf8','multibyte_oversized','short_read',
    'fifo','directory_leaf','ledger_mode','root_mode','root_symlink','ancestor_symlink',
    'missing','wrong_owner','wrong_dirfd',
])
def test_invalid_state_is_held_without_rewriting_or_repair(tmp_path, action):
    result, home, quota = probe(tmp_path, action, STATE_PROBE)
    assert result['status'] == 'held', result
    assert result['reserved'] == []
    assert not (home/'outside-new.json').exists()
    ledger=quota/'presence-eval-quota.json'
    if action in {'empty','malformed','array','duplicate','bool_used','bool_version',
                  'wrong_binding','negative_used','over_used','oversized','invalid_utf8','multibyte_oversized','short_read'}:
        # Rejection must preserve the original corrupt bytes, not reset them.
        assert ledger.read_bytes().hex() == result['original']
    if action in {'missing','wrong_owner','wrong_dirfd'}:
        assert not ledger.exists()
    assert all(n == 4097 for n in result['reads'])


def test_exact_byte_limit_is_read_bounded_and_charged(tmp_path):
    result, _, quota = probe(tmp_path,'exact_limit',STATE_PROBE)
    assert result == {'status':'allowed','reserved':[True],'reads':[4097],'original':None}
    assert json.loads((quota/'presence-eval-quota.json').read_bytes())['used']==1


def test_zero_budget_does_not_create_empty_resettable_ledger(tmp_path):
    result, _, quota = probe(tmp_path,'zero',STATE_PROBE)
    assert result['status']=='allowed', result
    assert result['reserved']==[False,True]
    assert json.loads((quota/'presence-eval-quota.json').read_bytes())['used']==1


RACE_PROBE = r'''
import fcntl,json,os,sys
from pathlib import Path
from scripts.eval_presence_current_turn import _reserve_isolated_call
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
home,quota=map(Path,sys.argv[1:3]);phase=sys.argv[3]
real_flock,real_read=fcntl.flock,os.read
paused=False
reads=[]
def pause():
    global paused
    if not paused:
        paused=True
        print('BARRIER',flush=True)
        assert sys.stdin.readline().strip()=='CONTINUE'
def flock(fd,op):
    real_flock(fd,op)
    if phase!='grow' and not __import__('stat').S_ISDIR(os.fstat(fd).st_mode): pause()
def read(fd,n):
    reads.append(n)
    if phase=='grow': pause()
    return real_read(fd,n)
fcntl.flock,os.read=flock,read
ReadOnlyEvaluationGuard(home=home,source=Path.cwd(),write_dirs=[quota]).install()
try: status='charged' if _reserve_isolated_call(quota,'SCRIPTED',3) else 'held'
except (OSError,ValueError): status='held'
print(json.dumps({'status':status,'reads':reads}))
'''


@pytest.mark.parametrize('action',['symlink','hardlink','regular','root_swap','grow'])
def test_swap_or_growth_after_open_never_mutates_an_outside_object(tmp_path,action):
    home,quota=tmp_path/'home',tmp_path/'quota'
    home.mkdir(mode=0o700);quota.mkdir(mode=0o700)
    seed=b'{"version":1,"binding":"SCRIPTED","used":0}'
    private=home/'private.json';private.write_bytes(seed);private.chmod(0o600)
    ledger=quota/'presence-eval-quota.json';ledger.write_bytes(seed);ledger.chmod(0o600)
    env={'PATH':str(Path(sys.executable).parent)+os.pathsep+os.defpath,'HOME':str(tmp_path),'CLOVER_HOME':str(home),'PYTHONPATH':str(ROOT),'PYTHONDONTWRITEBYTECODE':'1'}
    child=subprocess.Popen([sys.executable,'-B','-c',RACE_PROBE,str(home),str(quota),action],cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip()=='BARRIER'
        if action=='root_swap':
            quota.rename(tmp_path/'old-quota');quota.symlink_to(home,target_is_directory=True)
        elif action=='grow':
            with ledger.open('ab') as out: out.write(b' '*4097)
        else:
            ledger.rename(quota/'original.json')
            if action=='symlink': ledger.symlink_to(private)
            elif action=='hardlink': os.link(private,ledger)
            else: ledger.write_bytes(seed);ledger.chmod(0o600)
        stdout,stderr=child.communicate('CONTINUE\n',timeout=15)
        assert child.returncode==0,stderr
        result=json.loads(stdout)
        assert result['status']=='held',result
        assert private.read_bytes()==seed
        original=(tmp_path/'old-quota'/'presence-eval-quota.json' if action=='root_swap' else quota/'original.json')
        if action!='grow': assert original.read_bytes()==seed
        else: assert ledger.read_bytes()==seed+b' '*4097
        assert all(n==4097 for n in result['reads'])
    finally:
        if child.poll() is None: child.kill();child.communicate()


@pytest.mark.parametrize('existing',[False,True])
def test_concurrent_guarded_writers_share_one_hard_total(tmp_path,existing):
    home,quota=tmp_path/'home',tmp_path/'quota'
    home.mkdir(mode=0o700);quota.mkdir(mode=0o700)
    if existing:
        ledger=quota/'presence-eval-quota.json';ledger.write_text('{"version":1,"binding":"SCRIPTED","used":0}');ledger.chmod(0o600)
    code='''
import json,sys
from pathlib import Path
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
from scripts.eval_presence_current_turn import _reserve_isolated_call
home,quota=map(Path,sys.argv[1:3])
ReadOnlyEvaluationGuard(home=home,source=Path.cwd(),write_dirs=[quota]).install()
assert sys.stdin.readline().strip()=='GO'
print(json.dumps(_reserve_isolated_call(quota,'SCRIPTED',99)))
'''
    env={'PATH':os.defpath,'HOME':str(tmp_path),'CLOVER_HOME':str(home),'PYTHONPATH':str(ROOT),'PYTHONDONTWRITEBYTECODE':'1'}
    children=[subprocess.Popen([sys.executable,'-B','-c',code,str(home),str(quota)],cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(8)]
    try:
        for child in children:
            assert child.stdin is not None
            child.stdin.write('GO\n');child.stdin.flush()
        results=[]
        for child in children:
            stdout,stderr=child.communicate(timeout=15)
            assert child.returncode==0,stderr
            results.append(json.loads(stdout))
        assert results.count(True)==3 and results.count(False)==5,results
        assert json.loads((quota/'presence-eval-quota.json').read_text())['used']==3
        assert sorted(p.name for p in quota.iterdir())==['presence-eval-quota.json']
    finally:
        for child in children:
            if child.poll() is None: child.kill();child.communicate()

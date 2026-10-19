"""CI-only focused native reproduction. Never merge this helper branch."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / 'source'
OUT = ROOT / 'evidence'
OUT.mkdir(exist_ok=True)
PYTHON = SRC / '.venv/Scripts/python.exe'
BASH = Path('C:/Program Files/Git/bin/bash.exe')
SHA = '44ed73336bea1120eba1ae09a3f9cc8051e4208e'
TREE = 'ac3afa3692a7d1e152c79663dd3b34c57beb53b6'
def git(*args):
    return subprocess.check_output(['git', '-C', str(SRC), *args], text=True).strip()
assert git('rev-parse', 'HEAD') == SHA
assert git('rev-parse', 'HEAD^{tree}') == TREE
assert os.name == 'nt' and BASH.is_file()
receipt = dict(sha=SHA, tree=TREE, os=platform.platform(), python=str(PYTHON),
               status_before=git('status', '--porcelain', '--untracked-files=no'), rows=[])
(OUT / 'receipt.json').write_text(json.dumps(receipt, indent=2))

# Tight minimal children: independently vary only the two suspected locations.
# All socket audit events remain forbidden, including legitimate socket creation.
probe = '''import json,os,pathlib,sys
blocked=[]
def audit(event,args):
 if event.startswith('socket.'):
  blocked.append(event);raise AssertionError('network operation: '+event)
sys.addaudithook(audit)
data={'locations':{k:k in os.environ for k in ['HOME','USERPROFILE','HOMEDRIVE','HOMEPATH','SYSTEMROOT']}}
try:data['home']=str(pathlib.Path.home());data['home_ok']=True
except Exception as e:data['home_ok']=False;data['home_error']=type(e).__name__+': '+str(e)
try:import asyncio;data['import_ok']=True
except Exception as e:data['import_ok']=False;data['import_error']=type(e).__name__+': '+str(e)
data['blocked']=blocked
print(json.dumps(data))
'''
home = OUT / 'probe-home'
home.mkdir(exist_ok=True)
probes = []
for user in (False, True):
    for system in (False, True):
        env = {'HOME': str(home), 'PATH': '/usr/bin:/bin'}
        if user: env['USERPROFILE'] = str(home)
        if system: env['SYSTEMROOT'] = os.environ['SYSTEMROOT']
        p = subprocess.run([str(PYTHON), '-B', '-c', probe], cwd=SRC, env=env,
                           capture_output=True, text=True, timeout=30)
        probes.append(dict(userprofile=user, systemroot=system, returncode=p.returncode,
                           stdout=p.stdout, stderr=p.stderr))
(OUT / 'environment-probes.json').write_text(json.dumps(probes, indent=2))

# Reuse the existing reviewed offline network guard, with no source edits.
env = os.environ.copy()
for key in ['VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME']:
    env.pop(key, None)
env['HOME'] = str(Path(__file__).parent / 'home')
env['CLOVER_PYTHON'] = str(PYTHON)
selection = [
    ('tests/gateway/test_emotions_recheck_edges.py', 'test_new_cold_managed_inventory_before_import'),
    ('tests/gateway/test_trusted_human_origin_ingress.py', 'test_receipt_reads_back_in_a_fresh_process'),
    ('tests/test_presence_quality_evaluator.py', 'test_dry_cli_imports_no_runtime_auth_db_or_network'),
]
for index, (file, keyword) in enumerate(selection):
    cmd = [str(BASH), 'scripts/run_tests.sh', file, '-j', '1', '--file-timeout', '240',
           '--file-retries', '0', '--', '-q', '-p', 'no:randomly', '--tb=short', '-ra',
           '-k', keyword, '--junitxml=' + str(OUT / f'file-{index}.xml')]
    row = dict(file=file, command=cmd, started=time.time())
    with (OUT / f'file-{index}.log').open('w', encoding='utf-8') as log:
        row['exit_code'] = subprocess.run(cmd, cwd=SRC, env=env, stdout=log,
                                          stderr=subprocess.STDOUT).returncode
    row['ended'] = time.time()
    receipt['rows'].append(row)
    (OUT / 'receipt.json').write_text(json.dumps(receipt, indent=2))
receipt['status_after'] = git('status', '--porcelain', '--untracked-files=no')
(OUT / 'receipt.json').write_text(json.dumps(receipt, indent=2))
print(json.dumps(receipt, indent=2))
# Expected reproduction fails the workflow rather than repainting RED as success.
sys.exit(1 if any(row['exit_code'] for row in receipt['rows']) or receipt['status_after'] else 0)

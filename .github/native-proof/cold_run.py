"""CI transport only. Exact-source canonical guard acceptance, never merged."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
ROOT = Path.cwd()
SRC, DEST = ROOT/'source', ROOT/'evidence'
DEST.mkdir(exist_ok=True)
EXPECTED = 'a5d8d024212400ed7add239287b0569806c7ce4a'
TREE = '72491e1ad7a82e870a63e821d12fd3aeeb488c0f'
FILES = ['tests/test_presence_readonly_cold.py','tests/test_presence_eval_guard_descriptors.py']
def git(*args):
    return subprocess.check_output(['git','-C',str(SRC),*args],text=True).strip()
assert (git('rev-parse','HEAD'),git('rev-parse','HEAD^{tree}')) == (EXPECTED,TREE)
env = {k:v for k,v in os.environ.items() if k not in ('VIRTUAL_ENV','PYTHONPATH','PYTHONHOME')}
env.update(HOME=str(DEST/'user'), CLOVER_PYTHON=str(SRC/'.venv/bin/python'))
Path(env['HOME']).mkdir()
receipt = dict(sha=EXPECTED,tree=TREE,platform=platform.platform(),status_before=git('status','--porcelain','--untracked-files=no'),started=time.time(),invocations=[])
def save():
    (DEST/'receipt.json').write_text(json.dumps(receipt,indent=2))
save()
for index,file in enumerate(FILES):
    directory = DEST/f'file-{index}'
    directory.mkdir()
    xml = directory/'cases.xml'
    cmd = ['bash','scripts/run_tests.sh',file,'-j','1','--file-timeout','240','--file-retries','0','--','-q','-p','no:randomly','--tb=short','-ra','--junitxml='+str(xml)]
    start = time.monotonic()
    with (directory/'canonical.log').open('w') as output:
        run = subprocess.run(cmd,cwd=SRC,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT)
    row = dict(file=file,command=cmd,exit_code=run.returncode,seconds=time.monotonic()-start,cases=[])
    if xml.exists():
        row['cases']=[dict(name=c.attrib.get('name'),classname=c.attrib.get('classname'),failure=[f.text for f in c.findall('failure')],errors=[f.text for f in c.findall('error')],skips=[f.text for f in c.findall('skipped')]) for c in ET.parse(xml).iter('testcase')]
    receipt['invocations'].append(row)
    save()
receipt.update(ended=time.time(),status_after=git('status','--porcelain','--untracked-files=no'))
receipt['accepted']=not (receipt['status_before'] or receipt['status_after'] or any(r['exit_code'] or not r['cases'] or any(c['failure'] or c['errors'] or c['skips'] for c in r['cases']) for r in receipt['invocations']))
save()
print(json.dumps(receipt))
sys.exit(0 if receipt['accepted'] else 1)

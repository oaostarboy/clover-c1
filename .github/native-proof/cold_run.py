"""CI transport only. Pinned source, canonical guards, no retry."""
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
EXPECTED = '3283a21f1da61fc583eca9237eac87671d176c45'
TREE = 'b12278c3bbb7eb72ce822c6c39a66bc7b87fe421'
def git(*args):
    return subprocess.check_output(['git','-C',str(SRC),*args],text=True).strip()
assert (git('rev-parse','HEAD'),git('rev-parse','HEAD^{tree}')) == (EXPECTED,TREE)
env = {k:v for k,v in os.environ.items() if k not in ('VIRTUAL_ENV','PYTHONPATH','PYTHONHOME')}
env.update(HOME=str(DEST/'user'), CLOVER_PYTHON=str(SRC/'.venv/bin/python'))
Path(env['HOME']).mkdir()
cmd = ['bash','scripts/run_tests.sh','tests/test_presence_readonly_cold.py','-j','1','--file-timeout','240','--file-retries','0','--','-q','-p','no:randomly','--tb=short','-ra','-k','test_cold_full_evaluator_keeps_os_guard_through_actual_wire','--junitxml='+str(DEST/'cold.xml')]
receipt = dict(sha=EXPECTED,tree=TREE,platform=platform.platform(),command=cmd,status_before=git('status','--porcelain','--untracked-files=no'),started=time.time())
(DEST/'receipt.json').write_text(json.dumps(receipt,indent=2))
with (DEST/'canonical.log').open('w') as output:
    run = subprocess.run(cmd,cwd=SRC,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT)
receipt.update(exit_code=run.returncode,ended=time.time(),status_after=git('status','--porcelain','--untracked-files=no'))
if (DEST/'cold.xml').exists():
    receipt['cases']=[dict(name=c.attrib.get('name'),classname=c.attrib.get('classname'),failure=[f.text for f in c.findall('failure')],errors=[f.text for f in c.findall('error')]) for c in ET.parse(DEST/'cold.xml').iter('testcase')]
(DEST/'receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt))
sys.exit(run.returncode or bool(receipt['status_before'] or receipt['status_after']))

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
EXPECTED = '25177adf0ca7a4f6a13d60ec51cdbd0dde4d4ab9'
TREE = '883259e5e5aa07e18dd6a306b86252b97687598c'
def git(*args):
    return subprocess.check_output(['git','-C',str(SRC),*args],text=True).strip()
assert (git('rev-parse','HEAD'),git('rev-parse','HEAD^{tree}')) == (EXPECTED,TREE)
env = {k:v for k,v in os.environ.items() if k not in ('VIRTUAL_ENV','PYTHONPATH','PYTHONHOME')}
env.update(HOME=str(DEST/'user'), CLOVER_PYTHON=str(SRC/'.venv/bin/python'))
Path(env['HOME']).mkdir()
cmd = ['bash','scripts/run_tests.sh','tests/test_presence_eval_guard_descriptors.py','-j','1','--file-timeout','240','--file-retries','0','--','-q','-p','no:randomly','--tb=short','-ra','--junitxml='+str(DEST/'cold.xml')]
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

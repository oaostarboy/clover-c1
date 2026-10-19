"""Offline renderer ACK/state suite on the same frozen product head."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
ROOT=Path(__file__).resolve().parents[3]
SRC=ROOT/'source'
DEST=ROOT/'evidence'
DEST.mkdir(exist_ok=True)
def git(*args):
    return subprocess.check_output(['git','-C',str(SRC),*args],text=True).strip()
assert git('rev-parse','HEAD')=='d4735e9326ed109da6c31c23cd3419560ea1e714'
assert git('rev-parse','HEAD^{tree}')=='4436769e71d9ff56ecba1c53cf4dcaf6717f68b0'
receipt: dict[str, Any] = dict(sha=git('rev-parse','HEAD'),tree=git('rev-parse','HEAD^{tree}'),status_before=git('status','--porcelain','--untracked-files=no'),started=time.time())
(DEST/'receipt.json').write_text(json.dumps(receipt,indent=2))
home=DEST/'private-home';home.mkdir()
env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','COMSPEC','TEMP','TMP','LOCALAPPDATA','APPDATA','LANG'}}
env.update(HOME=str(home),USERPROFILE=str(home),CI='true')
command=['node',str(SRC/'node_modules/vitest/vitest.mjs'),'run','--project','ui','src/app/chat/presence-card.test.tsx','src/store/presence.test.ts','--maxWorkers','1','--no-file-parallelism','--reporter=json','--outputFile='+str(DEST/'vitest.json')]
with (DEST/'renderer.log').open('w') as log:
    result=subprocess.run(command,cwd=SRC/'apps/desktop',env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
receipt.update(command=command,exit_code=result.returncode,ended=time.time(),status_after=git('status','--porcelain','--untracked-files=no'))
if (DEST/'vitest.json').exists():
    output=json.loads((DEST/'vitest.json').read_text())
    receipt['counts']={key:output.get(key) for key in ['numTotalTestSuites','numPassedTestSuites','numFailedTestSuites','numTotalTests','numPassedTests','numFailedTests','numPendingTests','success']}
else:
    receipt['counts']=None
receipt['accepted']=result.returncode==0 and not receipt['status_before'] and not receipt['status_after'] and bool(receipt['counts']) and receipt['counts']['success'] and receipt['counts']['numFailedTests']==0 and receipt['counts']['numPendingTests']==0
(DEST/'receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt))
sys.exit(0 if receipt['accepted'] else 1)

"""Canonical exhaustive partitions with original 240s file cap and no retries."""
import collections
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
from typing import Any
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SRC, DEST = ROOT/'source', ROOT/'evidence'
DEST.mkdir(exist_ok=True)
FILES = [
 'tests/gateway/test_emotions_command.py',
 'tests/gateway/test_emotions_final_fence.py',
 'tests/gateway/test_emotions_independent_review.py',
 'tests/gateway/test_emotions_privacy_fixes.py',
 'tests/gateway/test_emotions_revision_reads.py',
 'tests/test_telegram_lossless_code.py',
 'tests/test_telegram_physical_delivery.py',
 'tests/tui_gateway/test_emotions_validation.py',
 'tests/tui_gateway/test_presence_integration.py',
 'tests/tui_gateway/test_presence_renderer_bridge.py',
 'tests/tui_gateway/test_presence_review_capabilities.py',
]
file = FILES[int(os.environ['FILE_INDEX'])]
def git(*args):
 return subprocess.check_output(['git','-C',str(SRC),*args],text=True).strip()
sha, tree = git('rev-parse','HEAD'), git('rev-parse','HEAD^{tree}')
assert sha == '44ed73336bea1120eba1ae09a3f9cc8051e4208e'
assert tree == 'ac3afa3692a7d1e152c79663dd3b34c57beb53b6'
python = SRC/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
env = os.environ.copy()
for key in ['VIRTUAL_ENV','PYTHONPATH']:
 env.pop(key,None)
env['HOME'] = str(Path(__file__).parent/'home')
env['CLOVER_PYTHON'] = str(python)
bash = str(Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Git/bin/bash.exe') if os.name=='nt' else 'bash'
base = [bash,'scripts/run_tests.sh',file,'-j','1','--file-timeout','240','--file-retries','0','--','-q','-p','no:randomly','--tb=short','-ra','-p','settlement_partition','-p','settlement_diagnostic']
receipt: dict[str, Any] = dict(sha=sha,tree=tree,file=file,os=platform.platform(),python=str(python),status_before=git('status','--porcelain','--untracked-files=no'),started=time.time(),batches=[])
def save():
 (DEST/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
save()
def run(label, extra):
 before=set(DEST.iterdir())
 cmd=base+extra
 log=DEST/(label+'.log')
 start=time.monotonic()
 with log.open('w',encoding='utf-8') as f:
  proc=subprocess.run(cmd,cwd=SRC,env=env,stdout=f,stderr=subprocess.STDOUT)
 directory=DEST/label
 directory.mkdir()
 for p in set(DEST.iterdir())-before-{directory}:
  if p.is_file(): shutil.move(str(p),directory/p.name)
 collections_found=[json.loads(p.read_text(encoding='utf-8')) for p in directory.glob('collection-*.json')]
 records=[]
 for p in directory.glob('*.json'):
  data=json.loads(p.read_text(encoding='utf-8'))
  if 'reports' in data: records.append(data)
 row: dict[str, Any]=dict(label=label,command=cmd,exit_code=proc.returncode,wall=time.monotonic()-start,collection=collections_found,records=[{'file':r['file'],'exitstatus':r.get('exitstatus'),'collected':r.get('collected')} for r in records])
 receipt['batches'].append(row)
 save()
 return row, records, directory
collection, _, _=run('collection',['--collect-only'])
# Canonical runner correctly exits1 for collect-only (zero executed tests).
# Accept discovery only when the inner pytest receipt is complete/exit0;
# never treat that discovery invocation as a passed execution.
assert collection['exit_code'] in (0,1) and len(collection['collection'])==1, 'collection missing'
assert len(collection['records'])==1 and collection['records'][0]['exitstatus']==0, 'collection failed'
all_ids=collection['collection'][0]['all_ids']
assert all_ids and len(all_ids)==len(set(all_ids))
receipt['all_ids']=all_ids
save()
# Original full owner file is kept together to reproduce predecessor effects.
size=len(all_ids) if file==FILES[0] else 8
executed=[]
failed=[]
issues=[]
for start in range(0,len(all_ids),size):
 stop=min(start+size,len(all_ids))
 row, records, directory=run('batch-'+str(start),['--case-start',str(start),'--case-stop',str(stop)])
 expected=all_ids[start:stop]
 if len(row['collection'])!=1 or row['collection'][0]['all_ids']!=all_ids or row['collection'][0]['selected_ids']!=expected:
  issues.append({'batch':start,'issue':'collection identity mismatch'})
 phases=[phase for rec in records for phase in rec['reports']]
 observed=sorted({phase['nodeid'] for phase in phases if phase.get('when')=='call' or (phase.get('when')=='setup' and phase.get('outcome') in ['failed','skipped'])})
 executed.extend(observed)
 failed.extend([{'nodeid':p['nodeid'],'when':p['when']} for p in phases if p.get('outcome')=='failed'])
 xml_counts=collections.Counter()
 for path in directory.glob('*.xml'):
  for case in ET.parse(path).iter('testcase'):
   xml_counts['testcases']+=1
   for tag in ['failure','error','skipped']:
    xml_counts[tag]+=len(case.findall(tag))
 row['xml_counts']=dict(xml_counts)
 row['expected_ids']=expected
 row['observed_ids']=observed
 if row['exit_code']!=0 or len(records)!=1 or records[0].get('exitstatus')!=0 or observed!=sorted(expected) or xml_counts['error'] or xml_counts['failure']:
  issues.append({'batch':start,'issue':'nonzero/incomplete/error execution; see raw canonical/XML/JSON'})
 save()
receipt.update(ended=time.time(),status_after=git('status','--porcelain','--untracked-files=no'),executed_ids=executed,failed_phases=failed,issues=issues,missing_ids=sorted(set(all_ids)-set(executed)),duplicate_ids=[k for k,n in collections.Counter(executed).items() if n>1])
receipt['accepted']=not(issues or failed or receipt['missing_ids'] or receipt['duplicate_ids'] or receipt['status_before'] or receipt['status_after'])
receipt['exit_code']=0 if receipt['accepted'] else 1
save()
print(json.dumps({k:v for k,v in receipt.items() if k not in ['all_ids','executed_ids','batches']}))
sys.exit(receipt['exit_code'])

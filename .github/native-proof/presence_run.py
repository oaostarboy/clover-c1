import hashlib,json,os,platform,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
SRC=ROOT/'source'
DEST=ROOT/'evidence'
DEST.mkdir(exist_ok=True)
def git(*args):return subprocess.check_output(['git','-C',str(SRC),*args],text=True).strip()
sha=git('rev-parse','HEAD');tree=git('rev-parse','HEAD^{tree}')
assert sha=='44ed73336bea1120eba1ae09a3f9cc8051e4208e'
assert tree=='ac3afa3692a7d1e152c79663dd3b34c57beb53b6'
manifest=json.loads((Path(__file__).parent/'presence-manifest.json').read_text())
python=SRC/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
env=os.environ.copy()
env.pop('VIRTUAL_ENV',None);env.pop('PYTHONPATH',None)
env['HOME']=str(Path(__file__).parent/'home')
env['CLOVER_PYTHON']=str(python)
scope=env['SCOPE']
# Never resolve bare bash.exe through PATH: native Windows exposes a WSL shim.
bash=str(Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Git/bin/bash.exe') if os.name=='nt' else 'bash'
if os.name=='nt': assert Path(bash).is_file(), 'Git for Windows Bash is required'
cmd=[bash,'scripts/run_tests.sh']
selection=['tests/gateway/test_emotions_command.py']
assert len(selection)==len(set(selection))==1
cmd+=selection
cmd+=['-j','1','--file-timeout','240','--file-retries','0','--','-q','-p','no:randomly','--tb=short','-ra','-k','test_status_owner_purges_expired_and_separates_exact_scope','-p','settlement_diagnostic']
receipt={'sha':sha,'tree':tree,'os':platform.platform(),'scope':scope,'command':cmd,'python':str(python),'status_before':git('status','--porcelain','--untracked-files=no'),'started':time.time()}
(DEST/'receipt.json').write_text(json.dumps(receipt,indent=2))
with (DEST/'canonical.log').open('w',encoding='utf-8') as f:
    proc=subprocess.Popen(cmd,cwd=SRC,env=env,stdout=f,stderr=subprocess.STDOUT)
    rc=proc.wait()
receipt.update(exit_code=rc,ended=time.time(),status_after=git('status','--porcelain','--untracked-files=no'))
records=[json.loads(p.read_text()) for p in DEST.glob('*.json') if p.name!='receipt.json' and 'reports' in json.loads(p.read_text())]
reports=[x for r in records for x in r.get('reports',[])]
receipt['files_with_reports']=len(records)
expected=set(selection)
receipt['expected_file_count']=len(expected)
receipt['missing_file_receipts']=sorted(expected-{r['file'] for r in records})
receipt['failed_phase_count']=sum(r['outcome']=='failed' for r in reports)
if receipt['missing_file_receipts'] or receipt['failed_phase_count'] or receipt['status_before'] or receipt['status_after']: rc=receipt['exit_code']=1
receipt['call_passed']=sum(r['when']=='call' and r['outcome']=='passed' for r in reports)
receipt['failed_phases']=[{'nodeid':r['nodeid'],'when':r['when']} for r in reports if r['outcome']=='failed']
receipt['skipped_phases']=sum(r['outcome']=='skipped' for r in reports)
(DEST/'receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps({k:v for k,v in receipt.items() if k not in ('command','failed_phases')}))
print('failed phases',len(receipt['failed_phases']))
sys.exit(rc)

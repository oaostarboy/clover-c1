"""Exact-source narrow native receipts, isolated fixtures, canonical runner."""
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

W=Path.cwd(); H=Path(__file__).resolve().parent; S=W/'source'; E=W/'evidence'
E.mkdir(exist_ok=True)
PY=S/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
def git(*args, root=S):
    return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
sha,tree=git('rev-parse','HEAD'),git('rev-parse','HEAD^{tree}')
assert sha==os.environ['TARGET_SHA'] and tree==os.environ['TARGET_TREE']
assert git('diff','--exit-code')==''
assert git('status','--porcelain','--untracked-files=no')==''
(E/'source.json').write_text(json.dumps({'sha':sha,'tree':tree,'helper_sha':git('rev-parse','HEAD',root=W/'helpers'),'platform':platform.platform(),'python':str(PY),'python_version':subprocess.check_output([str(PY),'--version'],text=True),'live_requests':0,'retries':0},indent=2))
files=['tests/test_presence_eval_quota_unsupported.py'] if os.name=='nt' else [
    'tests/test_presence_eval_quota_boundary.py','tests/test_presence_eval_quota_state.py',
    'tests/test_presence_eval_quota_unsupported.py','tests/test_presence_eval_guard_descriptors.py',
    'tests/test_presence_readonly_cold.py','tests/test_presence_isolated_evaluator.py',
    'tests/clover_cli/test_runtime_provider_readonly.py','tests/clover_cli/test_config_snapshot.py']
files=(['tests/test_presence_eval_quota_boundary.py','tests/test_presence_eval_quota_state.py']
       if os.environ.get('INVENTORY_ONLY')=='true' else files)
env={k:v for k,v in os.environ.items() if k in ('PATH','SYSTEMROOT','SystemRoot','USERPROFILE','HOMEDRIVE','HOMEPATH','LOCALAPPDATA','APPDATA','TEMP','TMP','PSModulePath','PSMODULEPATH')}
env.update(HOME=str(E/'home'),CLOVER_PYTHON=str(PY),TZ='UTC',LANG='C.UTF-8',PYTHONDONTWRITEBYTECODE='1')
rows=[]; failed=False
for index,file in enumerate(files):
    out=E/('test-'+str(index));out.mkdir(exist_ok=True)
    cmd=['bash','scripts/run_tests.sh',file,'-j','1','--file-timeout','180','--file-retries','0','--','-q','--tb=short','--junitxml='+str(out/'cases.xml')]
    with (out/'canonical.log').open('w') as log:
        run=subprocess.run(cmd,cwd=S,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=220)
    cases=[]
    if (out/'cases.xml').exists():
        for c in ET.parse(out/'cases.xml').getroot().iter('testcase'):
            cases.append({'id':c.get('classname')+'::'+c.get('name'),'failure':[n.text for n in c.findall('failure')],'error':[n.text for n in c.findall('error')],'skip':[n.get('message') for n in c.findall('skipped')]})
    rows.append({'file':file,'command':cmd,'exit':run.returncode,'cases':cases})
    (E/'tests.json').write_text(json.dumps(rows,indent=2))
    print(file,run.returncode,len(cases),flush=True)
    failed |= run.returncode!=0 or not cases
    if run.returncode: print((out/'canonical.log').read_text()[-6000:])

aliases=[]
if os.name!='nt' and os.environ.get('INVENTORY_ONLY')!='true':
    assert git('rev-parse','HEAD',root=W/'baseline')=='a5d8d024212400ed7add239287b0569806c7ce4a'
    for source_name,cases in [('descriptor_alias_controls.py',['symlink_fd','symlink_open','hardlink_fd','escape_dotdot','pipe_write_disguised']),('quota_controls.py',['ordinary','hardlink'])]:
        data=(H/source_name).read_bytes()
        module=ast.parse(data)
        probe=next(ast.literal_eval(n.value) for n in module.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='PROBE' for t in n.targets))
        for target in ['baseline','source']:
            for case in cases:
                with tempfile.TemporaryDirectory() as tmp:
                    base=Path(tmp).resolve();home,quota=base/'home',base/'quota'
                    home.mkdir(mode=0o700);quota.mkdir(mode=0o700)
                    child_env={'PATH':str(PY.parent)+os.pathsep+os.defpath,'HOME':str(base),'CLOVER_HOME':str(home),'PYTHONDONTWRITEBYTECODE':'1','LANG':'C.UTF-8'}
                    run=subprocess.run([str(PY),'-B','-c',probe,str(home),str(quota),case],cwd=W/target,env=child_env,capture_output=True,text=True,timeout=20)
                    row={'source':target,'probe':source_name,'probe_file_sha256':hashlib.sha256(data).hexdigest(),'probe_sha256':hashlib.sha256(probe.encode()).hexdigest(),'case':case,'exit':run.returncode,'stdout':run.stdout,'stderr':run.stderr,'files':{str(p.relative_to(base)):p.read_text() for p in base.rglob('*.json') if p.is_file()}}
                    aliases.append(row)
                    (E/'original-probes.json').write_text(json.dumps(aliases,indent=2))
                    if source_name=='descriptor_alias_controls.py':
                        expected='allowed' if target=='baseline' and case in ('symlink_open','hardlink_fd','escape_dotdot') else 'denied'
                        assert run.returncode==0 and json.loads(run.stdout)['status']==expected,row
                        assert row['files']['home/private.json']==('changed' if expected=='allowed' and case!='hardlink_fd' else 'changedl' if expected=='allowed' else 'original'),row
                    elif case=='ordinary':
                        assert run.returncode==0 and json.loads(run.stdout)['reserved']==[True,True,True,False],row
                    elif target=='baseline':
                        assert run.returncode==0 and json.loads(row['files']['home/private-ledger.json'])['used']==3,row
                    else:
                        assert run.returncode!=0 and json.loads(row['files']['home/private-ledger.json'])['used']==0,row
assert git('diff','--exit-code')==''
summary={'sha':sha,'tree':tree,'tests':sum(len(r['cases']) for r in rows),'failures':sum(bool(c['failure'] or c['error']) for r in rows for c in r['cases']),'skips':sum(bool(c['skip']) for r in rows for c in r['cases']),'original_probe_rows':len(aliases),'success':not failed,'live_requests':0,'retries':0}
(E/'result.json').write_text(json.dumps(summary,indent=2))
(E/'sha256.json').write_text(json.dumps({str(p.relative_to(E)):hashlib.sha256(p.read_bytes()).hexdigest() for p in E.rglob('*') if p.is_file()},indent=2))
print(json.dumps(summary),flush=True)
sys.exit(int(failed))

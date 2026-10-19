"""Exercise real quota reservation only in temporary synthetic directories."""
import json, os, subprocess, tempfile
from pathlib import Path
A=Path(__file__).parent
PROBE=r'''
import os,sys,json
from pathlib import Path
from scripts.eval_presence_current_turn import _reserve_isolated_call
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
home,q=map(Path,sys.argv[1:3]);scenario=sys.argv[3]
if scenario=='hardlink':
    p=home/'private-ledger.json'
    p.write_text(json.dumps({'version':1,'binding':'synthetic-isolated-binding','used':0}))
    p.chmod(0o600)
    os.link(p,q/'presence-eval-quota.json')
g=ReadOnlyEvaluationGuard(home=home,source=Path.cwd(),write_dirs=[q]);g.install()
results=[_reserve_isolated_call(q,'synthetic-isolated-binding',3) for _ in range(4)]
print(json.dumps({'reserved':results,'receipt':g.receipt()}))
'''
env={'PATH':'/usr/bin:/bin','HOME':str(A/'home'),'PYTHONDONTWRITEBYTECODE':'1','LANG':'C.UTF-8'}
rows=[]
for tree in ['baseline','mac']:
    for scenario in ['ordinary','hardlink']:
        with tempfile.TemporaryDirectory(dir=A,prefix='quota-private-') as tmp:
            base=Path(tmp);home=base/'home';q=base/'quota'
            home.mkdir(mode=0o700);q.mkdir(mode=0o700)
            r=subprocess.run([str(A/'venv311/bin/python'),'-B','-c',PROBE,str(home),str(q),scenario],cwd=A/tree,env=env,capture_output=True,text=True,timeout=15)
            row={'tree':tree,'scenario':scenario,'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
            if r.returncode==0:row['result']=json.loads(r.stdout)
            for name,path in [('quota_after',q/'presence-eval-quota.json'),('private_after',home/'private-ledger.json')]:
                row[name]=path.read_text() if path.exists() else None
            rows.append(row)
(A/'quota-controls.json').write_text(json.dumps(rows,indent=2))
for r in rows:print(r['tree'],r['scenario'],r['exit_code'],r.get('result',{}).get('reserved'),r['private_after'])

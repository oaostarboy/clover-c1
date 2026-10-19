"""Private object/alias counterexamples, no network or external HOME access."""
from pathlib import Path
import json, os, subprocess, tempfile
A=Path(__file__).parent
PROBE=r'''
import os,sys,json,stat,fcntl
from pathlib import Path
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
home=Path(sys.argv[1]);q=Path(sys.argv[2]);action=sys.argv[3]
private=home/'private.json';private.write_text('original');private.chmod(0o600)
path=q/'presence-eval-quota.json'
if action in ('symlink_fd','symlink_open'):path.symlink_to(private)
elif action=='hardlink_fd':os.link(private,path)
else:path.write_text('original')
if action in ('symlink_fd','hardlink_fd'):fd=os.open(path,os.O_RDWR)
else:fd=None
g=ReadOnlyEvaluationGuard(home=home,source=Path.cwd(),write_dirs=[q]);g.install()
status='allowed'
try:
    if action=='symlink_open':
        with open(path,'w') as s:s.write('changed')
    elif action in ('symlink_fd','hardlink_fd'):
        with os.fdopen(fd,'r+') as s:s.write('changed')
        fd=None
    elif action=='escape_dotdot':
        with open(q/'..'/'home'/'private.json','w') as s:s.write('changed')
    elif action=='pipe_write_disguised':
        r,w=os.pipe()
        try:g.audit('open',(w,'r',os.O_RDONLY))
        finally:os.close(r);os.close(w)
except PermissionError:status='denied'
if fd is not None:
    try:os.close(fd)
    except OSError:pass
print(json.dumps({'action':action,'status':status,'guard':g.receipt()}))
'''
env={'PATH':'/usr/bin:/bin','HOME':str(A/'home'),'PYTHONDONTWRITEBYTECODE':'1','LANG':'C.UTF-8'}
rows=[]
for tree in ['baseline','mac']:
    for action in ['symlink_fd','symlink_open','hardlink_fd','escape_dotdot','pipe_write_disguised']:
        with tempfile.TemporaryDirectory(dir=A,prefix='descriptor-') as tmp:
            base=Path(tmp);(base/'home').mkdir(mode=0o700);(base/'quota').mkdir(mode=0o700)
            r=subprocess.run([str(A/'venv311/bin/python'),'-B','-c',PROBE,str(base/'home'),str(base/'quota'),action],cwd=A/tree,env=env,capture_output=True,text=True,timeout=15)
            rows.append({'tree':tree,'action':action,'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr,'private_after':(base/'home/private.json').read_text()})
(A/'descriptor-alias-controls.json').write_text(json.dumps(rows,indent=2))
for r in rows:
    print(r['tree'],r['action'],r['exit'],r['stdout'].strip() or r['stderr'][-500:])

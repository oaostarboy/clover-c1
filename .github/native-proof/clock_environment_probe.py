"""Read-only disposable Windows clock-query environment experiment, not acceptance."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);a=p.parse_args()
assert os.name=='nt' and os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
a.evidence.mkdir(parents=True,exist_ok=True)
# Reproduce the canonical Bash allowlist literally, including Bash case behavior.
canonical=r'''WIN_ENV=(); for v in USERPROFILE HOMEDRIVE HOMEPATH LOCALAPPDATA APPDATA SYSTEMROOT TEMP TMP; do if [ -n "${!v:-}" ]; then WIN_ENV+=("$v=${!v}"); fi; done; exec env -i PATH="$PATH" HOME="$HOME" "${WIN_ENV[@]}" TZ=UTC LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONHASHSEED=0 PYTHONUTF8=1 PYTHON -c 'import os,json;print(json.dumps(dict(os.environ)))' '''
canonical=canonical.replace('PYTHON -c',shlex.quote(sys.executable.replace('\\','/'))+' -c')
shell=Path(os.environ['ProgramFiles'])/'Git/bin/bash.exe'
clean=json.loads(subprocess.check_output([str(shell),'-c',canonical],text=True,encoding='utf-8'))
child=r'''
import sys,os,time,json
sys.path.insert(0,sys.argv[1])
from clover_cli import windows_update_receipt_handoff as b
result={'python':sys.version,'pid':os.getpid()}; start=time.monotonic()
try:
 result['chain']=b.process_chain(os.getpid(),timeout=5); result['status']='success'
except Exception as e:
 result['status']=type(e).__name__
result['elapsed']=time.monotonic()-start
# Trace exactly the same bounded PowerShell command only when baseline failed.
if result['status']!='success':
 saved=b.subprocess.run; observed={}
 def trace(args,**kw):
  command=args[-1].replace('$all =',"Write-Output 'PHASE:before-cim'\n$all =").replace('$cursor = CURRENT_PID', '$cursor = CURRENT_PID')
  command=command.replace('$seen = @{}',"Write-Output 'PHASE:after-cim'\n$seen = @{}")
  try:
   r=saved([*args[:-1],command],**kw)
   observed['stages']=[x for x in r.stdout.splitlines() if x.startswith('PHASE:')]
   return r
  except Exception as e:
   output=getattr(e,'stdout',None) or getattr(e,'output',None) or ''
   if isinstance(output,bytes):output=output.decode('utf-8',errors='replace')
   observed['stages']=[x for x in output.splitlines() if x.startswith('PHASE:')]
   observed['status']=type(e).__name__
   raise
 b.subprocess.run=trace
 try:b.process_chain(os.getpid(),timeout=5)
 except Exception:pass
 finally:b.subprocess.run=saved
 result['trace']=observed
print(json.dumps(result))
'''
rows=[]
variants=[('canonical',clean)]
for key in ['WINDIR','PSModulePath','COMSPEC','COMPUTERNAME','SystemRoot']:
 value=os.environ.get(key)
 if value is not None:variants.append(('canonical+'+key,{**clean,key:value}))
variants.append(('full',dict(os.environ)))
for label,env in variants:
 started=time.monotonic()
 try:
  r=subprocess.run([sys.executable,'-c',child,str(a.source)],env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
  try:d=json.loads(r.stdout)
  except ValueError:d={'status':'no-json','returncode':r.returncode}
 except subprocess.TimeoutExpired:
  # Keep the hang observable and still run the later full-env control.
  # Never persist the exception's command, stdout, stderr or environment.
  d={'status':'outer-child-timeout','timeout_seconds':20}
 rows.append(dict(label=label,environment_keys=sorted(k for k in env if k.upper() in {'WINDIR','PSMODULEPATH','COMSPEC','COMPUTERNAME','SYSTEMROOT','USERPROFILE','LOCALAPPDATA','APPDATA','TEMP','TMP'}),result=d,outer_elapsed=time.monotonic()-started))
 (a.evidence/'environment-query.json').write_text(json.dumps(rows,indent=2))
 print(label,d.get('status'),d.get('elapsed'),flush=True)

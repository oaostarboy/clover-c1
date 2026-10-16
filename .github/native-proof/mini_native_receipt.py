"""Exact-source native receipt/runner cases only; not install/update acceptance."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);a=p.parse_args()
assert os.name=='nt' and os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
a.source=a.source.resolve();a.evidence=a.evidence.resolve();assert a.evidence.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve());a.evidence.mkdir(parents=True,exist_ok=True)
spec=importlib.util.spec_from_file_location('native_runner',a.source/'.github/native-proof/native_tests.py');assert spec and spec.loader
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
rows=[]
for label,file,count in [('native-clock','tests/clover_cli/test_windows_handoff_clock_native.py',3),('runner-env','tests/test_runner_windows_environment.py',2)]:
 xml=a.evidence/(label+'.xml');log=a.evidence/(label+'.log')
 command=mod.suite_command(a.source,file,'',xml)+['--file-timeout','120']
 start=time.monotonic();status='not-started';error=None;cases=None
 with log.open('w') as stream:
  try:status=subprocess.run(command,cwd=a.source,env={**os.environ,'CLOVER_PYTHON':sys.executable,'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH']},stdout=stream,stderr=subprocess.STDOUT,timeout=180).returncode
  except subprocess.TimeoutExpired:status='outer-timeout-180'
 try:
  cases=mod.validate_junit(xml)
  if cases!=count:error='case-count-mismatch'
 except Exception as e:error=type(e).__name__
 rows.append(dict(label=label,file=file,returncode=status,cases=cases,expected=count,validation_error=error,passed=status==0 and cases==count and error is None,elapsed=time.monotonic()-start,junit=xml.name,log=log.name,python=sys.version))
 (a.evidence/'SUMMARY.json').write_text(json.dumps(dict(results=rows,complete=len(rows)==2,all_passed=len(rows)==2 and all(r['passed'] for r in rows)),indent=2));print(label,status,cases,error,flush=True)
subprocess.run(['git','diff','--exit-code'],cwd=a.source,check=True)
with (a.evidence/'status.txt').open('w') as stream:subprocess.run(['git','status','--porcelain'],cwd=a.source,stdout=stream,check=True)
raise SystemExit(0 if all(r['passed'] for r in rows) else 1)

"""Actual guarded native pytest controls; diagnostic only, no release acceptance."""
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
a.source=a.source.resolve();a.evidence=a.evidence.resolve();assert a.evidence.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve())
a.evidence.mkdir(parents=True,exist_ok=True)
full=os.environ.get('PSModulePath');assert isinstance(full,str) and 0<len(full)<16384 and '\n' not in full and '\r' not in full
assert len(full.split(os.pathsep))<=16
stock=str(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/Modules')
original=a.source/'tests/clover_cli/test_windows_handoff_clock_native.py'
text=original.read_text();needle="    monkeypatch.setenv('PSModulePath', str(modules))";assert text.count(needle)==1
# An untracked, visibly named diagnostic copy retains the real repository guards.
# repr serializes a single path-list string; no raw data becomes executable code.
control=a.source/'tests/clover_cli/test_windows_handoff_clock_env_probe.py';assert not control.exists()
text=text.replace(needle,"    # DIAGNOSTIC CONTROL: actual full inherited OS path, not stock-only acceptance.\n    monkeypatch.setenv('PSModulePath', "+repr(full)+")")
control.write_text(text)
self_file=a.source/'tests/clover_cli/test_windows_handoff_self_env_probe.py';assert not self_file.exists()
self_file.write_text('import os,time,pytest\nfrom clover_cli import windows_update_receipt_handoff as bridge\n'+
 '@pytest.mark.parametrize("value", '+repr([stock,full])+', ids=["stock","full"])\n'+
 'def test_actual_current_process_query_control(value,monkeypatch,record_property):\n'+
 '    monkeypatch.setenv("PSModulePath",value)\n'+
 '    started=time.monotonic()\n'+
 '    chain=bridge.process_chain(os.getpid(),timeout=5)\n'+
 '    assert chain[0]["pid"]==os.getpid()\n'+
 '    record_property("query_elapsed",time.monotonic()-started)\n')
modpath=a.source/'.github/native-proof/native_tests.py';spec=importlib.util.spec_from_file_location('native_runner',modpath);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
rows=[]
for label,file in [('full-path-real-child',control),('stock-and-full-self',self_file)]:
 xml=a.evidence/(label+'.xml');log=a.evidence/(label+'.log')
 cmd=[mod.canonical_shell(),(a.source/'scripts/run_tests.sh').as_posix(),file.relative_to(a.source).as_posix(),'-j','1','--file-retries','0','--file-timeout','120','-o','addopts=','--junitxml='+xml.as_posix()]
 start=time.monotonic()
 with log.open('w') as stream:
  try:r=subprocess.run(cmd,cwd=a.source,env={**os.environ,'CLOVER_PYTHON':sys.executable,'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH']},stdout=stream,stderr=subprocess.STDOUT,timeout=180);status=r.returncode
  except subprocess.TimeoutExpired:status='outer-timeout-180'
 rows.append(dict(label=label,returncode=status,elapsed=time.monotonic()-start,junit=xml.name,log=log.name,python=sys.version))
 (a.evidence/'controls.json').write_text(json.dumps(rows,indent=2));print(label,status,flush=True)
# Only these untracked control files may differ; tracked source must stay exact.
subprocess.run(['git','diff','--exit-code'],cwd=a.source,check=True)
with (a.evidence/'status.txt').open('w') as stream:subprocess.run(['git','status','--porcelain'],cwd=a.source,stdout=stream,check=True)

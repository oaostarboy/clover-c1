"""Disposable Windows npm diagnostic; real commands, no updater exemptions."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

assert sys.platform == 'win32'
assert os.environ.get('GITHUB_ACTIONS') == 'true'
assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
repo, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
out.mkdir(parents=True, exist_ok=True)
keep = {'PATH','HOME','TMPDIR','RUNNER_TEMP','GITHUB_ACTIONS','RUNNER_ENVIRONMENT',
        'GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_SHA','SSL_CERT_FILE','SSL_CERT_DIR',
        'SystemRoot','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','USERPROFILE',
        'APPDATA','LOCALAPPDATA','USERDOMAIN','USERNAME','PROGRAMFILES','PROGRAMFILES(X86)',
        'PSModuleAnalysisCachePath'}
env = {k:v for k,v in os.environ.items() if k in keep}
env['CI'] = '1'
results = []
for tool in ['node','npm']:
    executable = shutil.which(tool)
    assert executable, tool + ' absent from hosted runner'
    began = time.monotonic()
    try:
        run = subprocess.run([executable,'--version'], cwd=repo, env=env, capture_output=True,
                             text=True, timeout=30)
        results.append(dict(tool=tool,executable=executable,exit=run.returncode,
                            stdout=run.stdout,stderr=run.stderr,elapsed=time.monotonic()-began))
    except subprocess.TimeoutExpired as exc:
        results.append(dict(tool=tool,executable=executable,timeout=True,elapsed=time.monotonic()-began,
                            stdout=str(exc.stdout),stderr=str(exc.stderr)))
    (out/'versions.json').write_text(json.dumps(results,indent=2))
    assert results[-1].get('exit') == 0, results[-1]
lock = repo/'package-lock.json'
before = hashlib.sha256(lock.read_bytes()).hexdigest()
cmd = [shutil.which('npm'),'ci','--include=dev','--no-fund','--no-audit','--prefer-offline',
       '--progress=false','--workspace','ui-tui','--workspace','web','--include-workspace-root']
began = time.monotonic()
with (out/'npm.log').open('w') as log:
    try:
        result = subprocess.run(cmd,cwd=repo,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=240)
        exit_code, timed_out = result.returncode, False
    except subprocess.TimeoutExpired:
        exit_code, timed_out = 124, True
npm_logs = Path(os.environ['LOCALAPPDATA'])/'npm-cache/_logs'
if npm_logs.exists():
    destination = out/'npm-debug';destination.mkdir(exist_ok=True)
    for file in npm_logs.glob('*.log'):
        shutil.copy2(file,destination/file.name)
record = dict(exit=exit_code,timeout=timed_out,elapsed=time.monotonic()-began,
              lock_before=before,lock_after=hashlib.sha256(lock.read_bytes()).hexdigest())
(out/'npm-result.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record))
assert record['lock_before'] == record['lock_after']
raise SystemExit(exit_code)

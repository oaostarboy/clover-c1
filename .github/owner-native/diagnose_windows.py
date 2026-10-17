"""Bounded Windows diagnosis. Original product and native fixture unchanged."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('exact_native_driver', HERE/'run.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
assert os.name == 'nt', 'real native Windows required'
initial = driver.identity('diagnostic-before')
for path in initial['tracked_sha256']:
    p = driver.SOURCE/path
    p.chmod(stat.S_IMODE(p.stat().st_mode) & ~0o222)
home = driver.OUT/'diagnostic-synthetic-home'
guard = home/'.clover'
guard.mkdir(parents=True, exist_ok=True)
(guard/'pytest_live_guard.py').write_text('"""Additional offline restrictions, never a live permission override."""\n')
(guard/'sitecustomize.py').write_text('import sys\ndef guard(e,a):\n    if e=="socket.connect" and isinstance(a[1],tuple) and a[1][0] not in ("127.0.0.1","::1","localhost"):\n        raise OSError("offline diagnostic guard")\n    if e=="socket.getaddrinfo" and a[0] not in (None,"127.0.0.1","::1","localhost"):\n        raise OSError("offline diagnostic guard")\nsys.addaudithook(guard)\n')
env = {k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','LOCALAPPDATA','APPDATA','PSMODULEPATH','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432'}}
env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8='1', TZ='UTC', TERM='xterm-256color')
# These are separate diagnostic selections, not retries of the canonical gate.
# Keep the original 300s gate timeout untouched and LOWER these probes to 60s.
selections = [
 ('permission-stack', 'test_native_permission_denial_and_private_directory_contract',1),
 ('console', 'test_native_console_hidden_confirmation_and_cancellation',2),
 ('remaining-native', 'not test_native_permission_denial_and_private_directory_contract and not test_native_console_hidden_confirmation_and_cancellation',7),
]
rows=[]
for label, selector, expected in selections:
    xml=driver.OUT/(label+'.xml')
    command=[shutil.which('bash'), str(driver.SOURCE/'scripts/run_tests.sh'), '-j','1','--file-retries','0','--file-timeout','60',str(HERE/'test_native_owner.py'),'-k',selector,'-p','no:randomly','-o','faulthandler_timeout=15','-o','junit_logging=all','--junitxml='+xml.as_posix(),'-r','a','--tb=short']
    with (driver.OUT/(label+'.log')).open('w',encoding='utf-8') as f:
        result=subprocess.run(command,cwd=driver.SOURCE,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=90)
    cases=[]
    if xml.exists():
        for tc in ET.parse(xml).getroot().iter('testcase'):
            status=next((x for x in ['failure','error','skipped'] if tc.find(x) is not None),'passed')
            cases.append({'name':tc.get('name'),'status':status,'detail': ''.join(tc.find(status).itertext()) if status!='passed' else ''})
    rows.append({'label':label,'command':command,'exit':result.returncode,'expected':expected,'cases':cases,'junit':xml.name if xml.exists() else None})
    driver.save('windows-diagnostics.json',rows)
    print(json.dumps(rows[-1]),flush=True)
final=driver.identity('diagnostic-after')
assert initial['tracked_sha256']==final['tracked_sha256']
# A retained timeout is evidence, never relabeled success or a repaired product.

"""Reproduce focused portable checks; never dispatch CI or touch Windows accounts."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.review/profile-diagnostic'
BASE = OUT / 'baseline-tree'
PROOF = Path('.github/owner-worker-proof')
EXISTING = ['test_contract.py', 'test_disposable_account.py',
            'test_restricted_token.py', 'test_bootstrap_resolution.py']
BASELINE = '8ab74772c5187f74600994dcd31059b5b998b4a3'
# Reconstruct exact immutable helper bytes inside this private worktree.
for name in subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', BASELINE, str(PROOF)], cwd=ROOT, text=True).splitlines():
    destination = BASE / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(subprocess.check_output(['git', 'cat-file', '--filters', BASELINE + ':' + name], cwd=ROOT))
for name in ('test_profile_diagnostics.py', 'test_profile_diagnostics.ps1'):
    shutil.copyfile(ROOT / PROOF / name, BASE / PROOF / name)
(OUT / 'tmp').mkdir(exist_ok=True)
env = dict(os.environ, CLOVER_PYTHON=sys.executable,
           PATH=str(OUT / 'powershell') + os.pathsep + os.environ['PATH'],
           TMP=str(OUT / 'tmp'), TEMP=str(OUT / 'tmp'),
           XDG_CACHE_HOME=str(OUT / 'tmp/cache'))
records = []
for label, prefix in [('baseline', BASE.relative_to(ROOT)), ('candidate', Path('.'))]:
    for name in EXISTING + ['test_profile_diagnostics.py']:
        stem = label + '-' + Path(name).stem
        xml = OUT / (stem + '.xml')
        log = OUT / (stem + '.log')
        command = ['bash', 'scripts/run_tests.sh', str(prefix / PROOF / name),
                   '-j', '1', '--file-retries', '0', '--', '--junitxml=' + str(xml)]
        with log.open('w') as stream:
            run = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
        cases = ET.parse(xml).findall('.//testcase')
        row = dict(label=label, file=name, command=command, exit=run.returncode,
                   cases=len(cases), failed=sum(c.find('failure') is not None for c in cases),
                   errors=sum(c.find('error') is not None for c in cases),
                   skipped=sum(c.find('skipped') is not None for c in cases),
                   ids=sorted('.'.join(c.attrib.get('classname', '').split('.')[-2:]) + '::' + c.attrib['name'] for c in cases),
                   junit_sha256=hashlib.sha256(xml.read_bytes()).hexdigest())
        records.append(row)
        expected_red = label == 'baseline' and name == 'test_profile_diagnostics.py'
        assert run.returncode == (1 if expected_red else 0), row
        assert row['failed'] == (1 if expected_red else 0) and row['errors'] == 0 and row['skipped'] == 0, row
        print(stem, 'exit=' + str(run.returncode), 'cases=' + str(len(cases)), flush=True)
for name in EXISTING:
    a, b = [r for r in records if r['file'] == name]
    assert a['ids'] == b['ids'], name
assert sum(r['cases'] for r in records if r['label'] == 'baseline' and r['file'] in EXISTING) == 38
assert sum(r['cases'] for r in records if r['label'] == 'candidate') == 40
(OUT / 'CHECKS.json').write_text(json.dumps(dict(scope='portable only; NO Windows proof', baseline=BASELINE, records=records), indent=2) + '\n')
print('Verified baseline existing 38/38; baseline diagnostic RED 1 failure/2; candidate 40/40; zero skips/errors')

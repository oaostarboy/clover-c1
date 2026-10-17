"""Windows-only diagnostic gate: direct Job API preflight, then exact console cases.

No product changes, retries, deadlines, backend assertions or cleanup edits.
The independent parent still decides whether to integrate this fixture.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import run as receipts

HELPER = Path(__file__).resolve().parent
OUT = receipts.OUT
SOURCE = receipts.SOURCE


def helper_identity(label):
    root = HELPER.parents[1]
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    record = {'head': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}'),
              'status': git('status', '--porcelain', '--untracked-files=no'),
              'replacement_refs': git('replace', '-l'),
              'sha256': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(HELPER.glob('*.py'))}}
    receipts.save(label + '-helper-identity.json', record)
    assert record['head'] == os.environ['GITHUB_SHA']
    assert not record['status'] and not record['replacement_refs']
    return record


def measured(name, args, env, timeout, cwd):
    started = time.monotonic()
    with (OUT / (name + '.log')).open('w', encoding='utf-8') as f:
        proc = subprocess.run(args, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT,
                              timeout=timeout)
    row = {'command': args, 'exit': proc.returncode, 'seconds': time.monotonic() - started,
           'log': name + '.log'}
    receipts.save(name + '.json', row)
    print(json.dumps(row), flush=True)
    return row


def main():
    assert os.name == 'nt', 'this diagnostic must run on real Windows'
    initial = receipts.identity('before')
    helper = helper_identity('before')
    try:
        dependencies = {d.metadata['Name']: d.version for d in importlib.metadata.distributions()}
        receipts.save('dependency-inventory.json', dependencies)
        assert importlib.metadata.version('pywin32') == '311'
        assert importlib.metadata.version('pywinpty') == '2.0.15'
        assert importlib.metadata.version('python-telegram-bot') == '22.8'
        fs = subprocess.run(['fsutil', 'fsinfo', 'volumeinfo', OUT.anchor.rstrip('\\/')],
                            capture_output=True, text=True)
        receipts.save('filesystem.json', {'command': fs.args, 'exit': fs.returncode,
                                         'stdout': fs.stdout, 'stderr': fs.stderr})
        assert fs.returncode == 0 and 'NTFS' in fs.stdout
        for path in initial['tracked_sha256']:
            p = SOURCE / path
            p.chmod(stat.S_IMODE(p.stat().st_mode) & ~0o222)
        home = OUT / 'synthetic-home'
        guard = home / '.clover'
        guard.mkdir(parents=True, exist_ok=True)
        (guard / 'pytest_live_guard.py').write_text('"""Offline diagnostic guard; no live permission overrides."""\n')
        (guard / 'sitecustomize.py').write_text('import sys\ndef guard(event,args):\n    if event == "socket.connect":\n        addr=args[1]\n        if isinstance(addr,tuple) and addr[0] not in ("127.0.0.1","::1","localhost"):\n            raise OSError("native owner fixture blocks external network")\n    if event == "socket.getaddrinfo" and args[0] not in (None,"localhost","127.0.0.1","::1"):\n        raise OSError("native owner fixture blocks external DNS")\nsys.addaudithook(guard)\n')
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','LOCALAPPDATA',
            'APPDATA','PSMODULEPATH','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432'}}
        env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8='1', TZ='UTC', TERM='xterm-256color')
        # Same outside-checkout editable-origin and offline checks as base run.py.
        probe_code = '''import json,socket,sys
import gateway.pairing,clover_cli.pairing
blocked=[]
for name,fn in [('connect',lambda:socket.create_connection(('198.51.100.1',443),timeout=1)),('dns',lambda:socket.getaddrinfo('example.invalid',443))]:
    try: fn()
    except OSError as exc:
        assert 'native owner fixture blocks external' in str(exc), repr(exc)
        blocked.append(name)
    else: raise AssertionError('external audit guard missing')
with socket.socket() as local: local.bind(('127.0.0.1',0))
print(json.dumps({'blocked':blocked,'pairing_origin':gateway.pairing.__file__,'cli_origin':clover_cli.pairing.__file__,'python':sys.executable}))
'''
        probe = subprocess.run([sys.executable, '-c', probe_code], cwd=OUT,
                               env={**env, 'PYTHONPATH': str(guard)}, capture_output=True,
                               text=True, timeout=20)
        receipts.save('offline-origin-preflight.json', {'exit': probe.returncode,
                                                       'stdout': probe.stdout, 'stderr': probe.stderr})
        assert probe.returncode == 0, probe.stderr
        observed = json.loads(probe.stdout)
        assert Path(observed['pairing_origin']).resolve() == SOURCE / 'gateway/pairing.py'
        assert Path(observed['cli_origin']).resolve() == SOURCE / 'clover_cli/pairing.py'
        receipts.save('plan.json', {'scope': 'Windows Job API then unchanged console cases only',
                                   'product': receipts.TARGET, 'product_tree': receipts.TREE,
                                   'helper': helper, 'job_preflight_expected': 5,
                                   'console_expected': 2, 'retries': 0,
                                   'console_deadlines': '20s prompt + 15s exit; 35s outer; cleanup unchanged',
                                   'not_exercised': ['full platform matrix', 'CLI timeout diagnosis', 'deployment']})
        row = measured('job-api-preflight', [sys.executable, str(HELPER / 'test_windows_job_api.py')],
                       {**env, 'PYTHONPATH': str(guard)}, 45, OUT)
        assert row['exit'] == 0, 'structural Job API error: full console cases NOT RUN'
        native = json.loads((OUT / row['log']).read_text(encoding='utf-8').splitlines()[-1])
        receipts.save('job-api-observations.json', native)
        assert native['tests'] == 5 and all(native[k] == 0 for k in ('failures','errors','skips'))
        bash = shutil.which('bash')
        assert bash and 'git' in bash.lower() and 'wsl' not in bash.lower(), bash
        xml = OUT / 'console.xml'
        args = [bash, str(SOURCE / 'scripts/run_tests.sh'), '-j', '1', '--file-retries', '0',
                str(HELPER / 'test_native_owner.py'), '-k',
                'test_native_console_hidden_confirmation_and_cancellation', '-p', 'no:randomly',
                '--junitxml=' + xml.as_posix(), '-r', 'a', '--tb=short', '-s']
        row = measured('console', args, env, 360, SOURCE)
        cases = []
        if xml.exists():
            for case in ET.parse(xml).getroot().iter('testcase'):
                outcome = next((x for x in ('failure','error','skipped') if case.find(x) is not None), 'passed')
                cases.append({'classname': case.get('classname'), 'name': case.get('name'),
                              'outcome': outcome,
                              'detail': ''.join(case.find(outcome).itertext()) if outcome != 'passed' else ''})
        receipts.save('console-cases.json', cases)
        assert len(cases) == 2, 'incomplete console XML'
        assert row['exit'] == 0 and all(c['outcome'] == 'passed' for c in cases), 'console gate failed; preserve finding'
    finally:
        final = receipts.identity('after')
        assert initial['tracked_sha256'] == final['tracked_sha256'], 'product tracked bytes changed'
        final_helper = helper_identity('after')
        assert helper == final_helper, 'helper tracked bytes changed'


if __name__ == '__main__':
    main()

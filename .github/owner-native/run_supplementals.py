"""Additive native coverage and helper receipts; original run.py is unchanged.

Run --before immediately before the full original gate, then --after even if
that gate fails. Never reinterpret a failed/missing original row as a pass.
No product edits, dependency installs, network grants, retries or new deadlines.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import run as core


def helper_identity(label):
    root = core.HELPER.parents[1]
    paths = sorted(core.HELPER.glob('*.py')) + [root / '.github/workflows/native-exact-proof.yml']
    value = {
        'head': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
        'tree': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD^{tree}'], text=True).strip(),
        'status': subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no'], text=True).strip(),
        'replacement_refs': subprocess.check_output(['git', '-C', str(root), 'replace', '-l'], text=True).strip(),
        'sha256': {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
    }
    core.save(label + '-helper-identity.json', value)
    assert not value['status'] and not value['replacement_refs'], 'helper source identity rejected'
    return value


def after():
    initial = json.loads((core.OUT / 'before-helper-identity.json').read_text(encoding='utf-8'))
    source_initial = json.loads((core.OUT / 'before-identity.json').read_text(encoding='utf-8'))
    rows = []
    errors = []
    try:
        assert helper_identity('before-supplemental') == initial, 'helper changed during original gate'
        # The unchanged canonical runner deletes each private pytest temp root.
        # Do not weaken that cleanup to retain passing profile/store contents.
        # Console metadata is validated inside run_console before the unchanged
        # full-transcript/no-echo/persistence assertions. The prior independent
        # focused run separately retains and verifies the two safe receipts.
        home = core.OUT / 'supplemental-home'
        guard = home / '.clover'
        guard.mkdir(parents=True, exist_ok=True)
        for name in ('sitecustomize.py', 'pytest_live_guard.py'):
            shutil.copyfile(core.OUT / 'synthetic-home/.clover' / name, guard / name)
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
            'PSMODULEPATH', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432'}}
        env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / 'AppData/Local'),
                   APPDATA=str(home / 'AppData/Roaming'), PYTHONUTF8='1', TZ='UTC', TERM='xterm-256color')
        for name in ('LOCALAPPDATA', 'APPDATA'):
            Path(env[name]).mkdir(parents=True, exist_ok=True)
        probe = subprocess.run([sys.executable, '-c', '''import json,socket
import gateway.pairing,clover_cli.pairing
for fn in [lambda:socket.create_connection(('198.51.100.1',443),timeout=1),lambda:socket.getaddrinfo('example.invalid',443)]:
    try: fn()
    except OSError as exc: assert 'native owner fixture blocks external' in str(exc)
    else: raise AssertionError('external guard absent')
print(json.dumps({'blocked':['connect','dns'],'pairing_origin':gateway.pairing.__file__,'cli_origin':clover_cli.pairing.__file__}))
'''], cwd=core.OUT, env={**env, 'PYTHONPATH': str(guard)}, capture_output=True, text=True, timeout=20)
        core.save('supplemental-preflight.json', {'exit': probe.returncode, 'stdout': probe.stdout, 'stderr': probe.stderr})
        assert probe.returncode == 0, 'offline supplemental preflight failed'
        origins = json.loads(probe.stdout)
        assert Path(origins['pairing_origin']).resolve() == core.SOURCE / 'gateway/pairing.py'
        assert Path(origins['cli_origin']).resolve() == core.SOURCE / 'clover_cli/pairing.py'
        files = [('tests/gateway/test_pairing_writer.py', 21)]
        if os.name == 'nt':
            files += [(str(core.HELPER / 'test_windows_job_api.py'), 5),
                      (str(core.HELPER / 'test_windows_console_transport.py'), 3)]
        core.save('supplemental-plan.json', {'files': files, 'workers': 1, 'file_retries': 0,
                  'original_files': core.FILES, 'file_timeout': 'canonical default unchanged',
                  'nonapplicable_on_macos': ['Windows Job API (5)', 'Windows ConPTY lifecycle (3)'],
                  'expected_writer_linux_only_skips': 5})
        bash = shutil.which('bash')
        assert bash and (os.name != 'nt' or ('git' in bash.lower() and 'wsl' not in bash.lower()))
        for i, (name, expected) in enumerate(files):
            xml = core.OUT / f'supp-{i:02d}-{Path(name).stem}.xml'
            log = core.OUT / f'supp-{i:02d}-{Path(name).stem}.log'
            assert not xml.exists() and not log.exists(), 'no evidence overwrite/retry'
            args = [bash, str(core.SOURCE / 'scripts/run_tests.sh'), '-j', '1', '--file-retries', '0', name,
                    '-p', 'no:randomly', '--junitxml=' + xml.as_posix(), '-r', 'a', '--tb=short']
            started = time.monotonic()
            timeout_error = None
            with log.open('x', encoding='utf-8') as output:
                try:
                    proc = subprocess.run(args, cwd=core.SOURCE, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=360)
                    exitcode = proc.returncode
                except subprocess.TimeoutExpired:
                    exitcode = None
                    timeout_error = 'original 360s outer bound exceeded; not acceptance'
            cases = []
            if xml.exists():
                for case in ET.parse(xml).getroot().iter('testcase'):
                    outcome = next((x for x in ('failure', 'error', 'skipped') if case.find(x) is not None), 'passed')
                    detail = '' if outcome == 'passed' else ''.join(case.find(outcome).itertext())
                    cases.append({'classname': case.get('classname'), 'name': case.get('name'), 'outcome': outcome, 'detail': detail})
            rows.append({'file': name, 'expected_cases': expected, 'exit': exitcode, 'timeout': timeout_error,
                         'seconds': time.monotonic() - started, 'cases': cases, 'command': args,
                         'xml': xml.name if xml.exists() else None, 'log': log.name})
            core.save('supplemental-results.json', rows)
            print(json.dumps({'file': name, 'exit': exitcode, 'counts': {s: sum(c['outcome'] == s for c in cases) for s in ('passed', 'failure', 'error', 'skipped')}}), flush=True)
        assert all(r['exit'] == 0 and len(r['cases']) == r['expected_cases'] for r in rows), 'additive gate incomplete/failed'
        assert all(c['outcome'] in ('passed', 'skipped') for r in rows for c in r['cases'])
        assert all(c['outcome'] == 'passed' for r in rows[1:] for c in r['cases']), 'Windows capability cases must not skip'
    except BaseException as exc:
        errors.append({'type': type(exc).__name__, 'message': str(exc)})
        raise
    finally:
        core.save('supplemental-errors.json', errors)
        try:
            final = core.identity('after-combined')
            assert source_initial['tracked_sha256'] == final['tracked_sha256'], 'combined product bytes changed'
        finally:
            assert helper_identity('after') == initial, 'combined helper bytes changed'


if __name__ == '__main__':
    assert sys.argv[1:] in (['--before'], ['--after'])
    if sys.argv[1] == '--before':
        helper_identity('before')
    else:
        after()

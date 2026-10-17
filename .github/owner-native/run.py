"""Exact-source native diagnostics, no install/update/service claims."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parents[3]
SOURCE = BASE / 'source'
HELPER = Path(__file__).resolve().parent
OUT = Path(os.environ.get('RUNNER_TEMP', str(BASE))) / 'owner-native-evidence'
OUT.mkdir(parents=True, exist_ok=True)
TARGET = '918cf7ae3d39556cb10102d282b7283dc486467b'
TREE = 'ad870be841e691f7a00f175ea8dca43c4c1b037e'
FILES = [
 'tests/test_verified_owner.py', 'tests/test_owner_setup.py',
 'tests/test_owner_telegram_wire.py', 'tests/test_owner_proactive_controls.py',
 'tests/test_owner_proof_envelope.py', 'tests/test_owner_proof_cleanup.py',
 'tests/gateway/test_pairing.py', 'tests/gateway/test_pairing_allowlist_bypass.py',
 'tests/gateway/test_proactive_command.py', 'tests/gateway/test_slash_access.py',
 'tests/gateway/test_slash_access_dispatch.py', str(HELPER / 'test_native_owner.py'),
]


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def git(*args):
    return subprocess.check_output(['git', '-C', str(SOURCE), *args], text=True).strip()


def identity(label):
    entries = subprocess.check_output(['git', '-C', str(SOURCE), 'ls-tree', '-rz', 'HEAD']).split(b'\0')
    mismatches = []
    checkout_transforms = []
    hashes = {}
    for e in filter(None, entries):
        meta, path = e.split(b'\t', 1)
        mode, kind, oid = meta.split()
        if kind != b'blob':
            continue
        p = SOURCE / os.fsdecode(path)
        content = p.read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        if actual != oid.decode():
            # Honor the reviewed .gitattributes checkout contract, including
            # deliberate PowerShell CRLF. Never rewrite or ignore a path.
            expected = subprocess.check_output(['git', '-C', str(SOURCE), 'cat-file', '--filters', 'HEAD:' + os.fsdecode(path)])
            if content != expected:
                mismatches.append(str(p.relative_to(SOURCE)))
            else:
                checkout_transforms.append({'path': os.fsdecode(path), 'blob': oid.decode(), 'checkout_sha256': hashlib.sha256(content).hexdigest(), 'attributes': git('check-attr', '--all', '--', os.fsdecode(path))})
        hashes[os.fsdecode(path)] = hashlib.sha256(content).hexdigest()
    result = {'head': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}'), 'replacement_refs': git('replace', '-l'), 'tracked_status': git('status', '--porcelain', '--untracked-files=no'), 'checkout_transforms': checkout_transforms, 'mismatches': mismatches, 'tracked_sha256': hashes, 'python': sys.version, 'platform': platform.platform(), 'helper_head': subprocess.check_output(['git', '-C', str(HELPER), 'rev-parse', 'HEAD'], text=True).strip()}
    save(label + '-identity.json', result)
    assert result['head'] == TARGET and result['tree'] == TREE
    assert not result['replacement_refs'] and not result['tracked_status'] and not mismatches, 'exact tracked-byte identity rejected; see receipt'
    return result


def main():
    initial = identity('before')
    save('dependencies.json', {x: importlib.metadata.version(x) for x in ['python-telegram-bot', 'pytest', 'pytest-asyncio', 'pywinpty' if os.name == 'nt' else 'ptyprocess']})
    save('dependency-inventory.json', {d.metadata['Name']: d.version for d in importlib.metadata.distributions()})
    filesystem = []
    if os.name == 'nt':
        commands = [['fsutil', 'fsinfo', 'volumeinfo', OUT.anchor.rstrip('\\/')]]
    elif sys.platform == 'darwin':
        df = subprocess.run(['df', '-P', str(OUT)], capture_output=True, text=True)
        filesystem.append({'command': df.args, 'exit': df.returncode, 'stdout': df.stdout, 'stderr': df.stderr})
        commands = [['diskutil', 'info', df.stdout.splitlines()[-1].split()[0]]] if df.returncode == 0 else []
    else:
        commands = [['df', '-T', str(OUT)]]
    for command in commands:
        measured = subprocess.run(command, capture_output=True, text=True)
        filesystem.append({'command': command, 'exit': measured.returncode, 'stdout': measured.stdout, 'stderr': measured.stderr})
    save('filesystem.json', filesystem)
    assert importlib.metadata.version('python-telegram-bot') == '22.8'
    # Read-only tracked files. Generated caches/homes are outside product source.
    for path in initial['tracked_sha256']:
        p = SOURCE / path
        p.chmod(stat.S_IMODE(p.stat().st_mode) & ~0o222)
    home = OUT / 'synthetic-home'
    guard = home / '.clover'
    guard.mkdir(parents=True, exist_ok=True)
    # The canonical runner automatically loads this ordinary test guard from
    # synthetic HOME. It does not disable or replace any shipped policy guard.
    (guard / 'pytest_live_guard.py').write_text('"""Offline diagnostic guard; no live permission overrides."""\n')
    (guard / 'sitecustomize.py').write_text('import sys\ndef guard(event,args):\n    if event == "socket.connect":\n        addr=args[1]\n        if isinstance(addr,tuple) and addr[0] not in ("127.0.0.1","::1","localhost"):\n            raise OSError("native owner fixture blocks external network")\n    if event == "socket.getaddrinfo" and args[0] not in (None,"localhost","127.0.0.1","::1"):\n        raise OSError("native owner fixture blocks external DNS")\nsys.addaudithook(guard)\n')
    env = {k: v for k, v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','LOCALAPPDATA','APPDATA','PSMODULEPATH','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432'}}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8='1', TZ='UTC', TERM='xterm-256color')
    save('plan.json', {'files': FILES, 'workers': 1, 'file_retries': 0, 'file_timeout': 'canonical default unchanged', 'console': 'native ConPTY on Windows, controlling PTY on macOS', 'expected_product_core_counts': [21,10,16,16,30,8], 'additional_native_cases': 10, 'not_claimed': ['installers','service readiness','live Telegram','providers','production deployment','private ACL provisioning']})
    probe_code = '''import json,socket,sys,importlib.metadata
from pathlib import Path
import gateway.pairing,clover_cli.pairing
blocked=[]
for name,fn in [('connect',lambda:socket.create_connection(('198.51.100.1',443),timeout=1)),('dns',lambda:socket.getaddrinfo('example.invalid',443))]:
    try: fn()
    except OSError as exc:
        assert 'native owner fixture blocks external' in str(exc), repr(exc)
        blocked.append(name)
    else: raise AssertionError('external audit guard missing')
with socket.socket() as local:
    local.bind(('127.0.0.1',0))
print(json.dumps({'blocked_before_network':blocked,'loopback_bind':True,'python':sys.executable,'pairing_origin':gateway.pairing.__file__,'cli_origin':clover_cli.pairing.__file__,'editable':json.loads(importlib.metadata.distribution('clover-c1').read_text('direct_url.json'))}))
'''
    probe = subprocess.run([sys.executable, '-c', probe_code], cwd=OUT, env={**env, 'PYTHONPATH': str(guard)}, capture_output=True, text=True, timeout=20)
    save('preflight.json', {'exit': probe.returncode, 'stdout': probe.stdout, 'stderr': probe.stderr})
    assert probe.returncode == 0, probe.stderr
    measured = json.loads(probe.stdout)
    assert Path(measured['pairing_origin']).resolve() == SOURCE / 'gateway/pairing.py'
    assert Path(measured['cli_origin']).resolve() == SOURCE / 'clover_cli/pairing.py'
    results = []
    bash = shutil.which('bash')
    assert bash
    if os.name == 'nt':
        assert 'git' in bash.lower() and 'wsl' not in bash.lower(), bash
    for i, name in enumerate(FILES):
        xml = OUT / f'{i:02d}-{Path(name).stem}.xml'
        log = OUT / f'{i:02d}-{Path(name).stem}.log'
        args = [bash, str(SOURCE / 'scripts/run_tests.sh'), '-j', '1', '--file-retries', '0', name, '-p', 'no:randomly', '--junitxml=' + xml.as_posix(), '-r', 'a', '--tb=short']
        if i == len(FILES) - 1:
            args.append('-s')  # native diagnostics print only safe OS metadata
        started = time.monotonic()
        with log.open('w', encoding='utf-8') as f:
            proc = subprocess.run(args, cwd=SOURCE, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=360)
        cases = []
        if xml.exists():
            for case in ET.parse(xml).getroot().iter('testcase'):
                outcome = next((x for x in ('failure','error','skipped') if case.find(x) is not None), 'passed')
                cases.append({'classname': case.get('classname'), 'name': case.get('name'), 'outcome': outcome, 'detail': ''.join(case.find(outcome).itertext()) if outcome != 'passed' else ''})
        row = {'file': name, 'expected_cases': [21,10,16,16,30,8,30,13,10,7,7,10][i], 'exit': proc.returncode, 'seconds': time.monotonic()-started, 'cases': cases, 'xml': xml.name if xml.exists() else None, 'log': log.name, 'command': args}
        results.append(row)
        save('test-results.json', results)
        print(json.dumps({'file': name, 'exit': proc.returncode, 'counts': {s: sum(c['outcome'] == s for c in cases) for s in ['passed','failure','error','skipped']}}), flush=True)
    final = identity('after')
    assert initial['tracked_sha256'] == final['tracked_sha256'], 'tracked bytes changed during execution'
    assert len(results) == len(FILES)
    assert all(len(row['cases']) == row['expected_cases'] for row in results), 'collected counts differ from frozen plan'
    assert all(row['exit'] == 0 and row['cases'] for row in results), 'one or more native rows failed; see per-file evidence'


if __name__ == '__main__':
    main()

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
TARGET = 'de9df79dad8e07ba27b4e6dd1c9c674701dca70b'
TREE = '23b47f3cc04be64bc3e93be16d2d092ec28c83a9'
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
            mismatches.append(str(p.relative_to(SOURCE)))
        hashes[os.fsdecode(path)] = hashlib.sha256(content).hexdigest()
    result = {'head': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}'), 'replacement_refs': git('replace', '-l'), 'tracked_status': git('status', '--porcelain', '--untracked-files=no'), 'mismatches': mismatches, 'tracked_sha256': hashes, 'python': sys.version, 'platform': platform.platform(), 'helper_head': subprocess.check_output(['git', '-C', str(HELPER), 'rev-parse', 'HEAD'], text=True).strip()}
    save(label + '-identity.json', result)
    assert result['head'] == TARGET and result['tree'] == TREE
    assert not result['replacement_refs'] and not result['tracked_status'] and not mismatches, 'exact tracked-byte identity rejected; see receipt'
    return result


def main():
    initial = identity('before')
    save('dependencies.json', {x: importlib.metadata.version(x) for x in ['python-telegram-bot', 'pytest', 'pytest-asyncio', 'pywinpty' if os.name == 'nt' else 'ptyprocess']})
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
    save('plan.json', {'files': FILES, 'workers': 1, 'file_retries': 0, 'file_timeout': 'canonical default unchanged', 'console': 'native ConPTY on Windows, controlling PTY on macOS', 'expected_product_core_counts': [21,10,16,16,30,8], 'additional_native_cases': 9, 'not_claimed': ['installers','service readiness','live Telegram','providers','production deployment','private ACL provisioning']})
    results = []
    bash = shutil.which('bash')
    assert bash
    if os.name == 'nt':
        assert 'git' in bash.lower() and 'wsl' not in bash.lower(), bash
    for i, name in enumerate(FILES):
        xml = OUT / f'{i:02d}-{Path(name).stem}.xml'
        log = OUT / f'{i:02d}-{Path(name).stem}.log'
        args = [bash, str(SOURCE / 'scripts/run_tests.sh'), '-j', '1', '--file-retries', '0', name, '-p', 'no:randomly', '--junitxml=' + xml.as_posix(), '-r', 'a', '--tb=short']
        started = time.monotonic()
        with log.open('w', encoding='utf-8') as f:
            proc = subprocess.run(args, cwd=SOURCE, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=360)
        cases = []
        if xml.exists():
            for case in ET.parse(xml).getroot().iter('testcase'):
                outcome = next((x for x in ('failure','error','skipped') if case.find(x) is not None), 'passed')
                cases.append({'classname': case.get('classname'), 'name': case.get('name'), 'outcome': outcome, 'detail': ''.join(case.find(outcome).itertext()) if outcome != 'passed' else ''})
        row = {'file': name, 'exit': proc.returncode, 'seconds': time.monotonic()-started, 'cases': cases, 'xml': xml.name if xml.exists() else None, 'log': log.name, 'command': args}
        results.append(row)
        save('test-results.json', results)
        print(json.dumps({'file': name, 'exit': proc.returncode, 'counts': {s: sum(c['outcome'] == s for c in cases) for s in ['passed','failure','error','skipped']}}), flush=True)
    identity('after')
    assert len(results) == len(FILES)
    assert all(row['exit'] == 0 and row['cases'] for row in results), 'one or more native rows failed; see per-file evidence'


if __name__ == '__main__':
    main()

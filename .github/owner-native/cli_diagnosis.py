"""Bounded Windows CLI diagnosis. Not the native release acceptance gate."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import psutil

HELPER = Path(__file__).resolve().parent
original = runpy.run_path(str(HELPER / 'run.py'))
SOURCE, OUT = original['SOURCE'], original['OUT']
save, identity = original['save'], original['identity']


def run_case(label, env, bash, observed):
    case_out = OUT / label
    case_out.mkdir()
    copied = {}
    templates = {'cli_probe_plugin.py': 'cli_probe_plugin.py'}
    if observed:
        templates['sitecustomize.py'] = 'cli_observer.py'
    for target, template in templates.items():
        path = SOURCE / target
        assert not path.exists(), 'refuse to overwrite existing observer path'
        content = (HELPER / template).read_bytes()
        if target == 'sitecustomize.py' and label == 'stdin-identity-experiment':
            content += b'\n    import io\n    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding=sys.stdin.encoding, errors=sys.stdin.errors)\n    emit("stdin_identity_experiment", stdin_is_original=sys.stdin is sys.__stdin__, stdin_isatty=sys.stdin.isatty())\n'
        path.write_bytes(content)
        copied[path] = hashlib.sha256(content).hexdigest()
    destination = SOURCE / 'owner_cli_observer_destination.txt'
    if observed:
        assert not destination.exists()
        destination.write_text(str(case_out), encoding='utf-8')
        copied[destination] = hashlib.sha256(destination.read_bytes()).hexdigest()
    args = [bash, str(SOURCE / 'scripts/run_tests.sh'), '-j', '1', '--file-retries', '0', 'tests/test_owner_setup.py', '-p', 'no:randomly', '-p', 'cli_probe_plugin', '--cli-observation-out=' + case_out.as_posix(), '--junitxml=' + (case_out / 'cases.xml').as_posix(), '-r', 'a', '--tb=short']
    started = time.monotonic()
    try:
        with (case_out / 'canonical.log').open('w', encoding='utf-8') as file:
            proc = subprocess.run(args, cwd=SOURCE, env=env, stdout=file, stderr=subprocess.STDOUT, timeout=180)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        exit_code = 'OUTER_TIMEOUT_UNRESOLVED'
    finally:
        for path, digest in copied.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
            path.unlink()
        # Do not let an orphaned child or its imported observer contaminate the next case.
        for name in ('sitecustomize', 'cli_probe_plugin'):
            cache = SOURCE / '__pycache__'
            for path in cache.glob(name + '.*.pyc'):
                path.unlink()
    events = [json.loads(line) for line in (case_out / 'parent.jsonl').read_text().splitlines()] if (case_out / 'parent.jsonl').exists() else []
    observed_pids = {e['pid'] for e in events if e['event'] == 'popen'}
    for path in case_out.glob('child-*.jsonl'):
        observed_pids.add(int(path.stem.split('-')[1]))
    living = []
    for pid in sorted(observed_pids):
        alive = psutil.pid_exists(pid)
        record = {'pid': pid, 'exists_after_case': alive}
        if alive:
            child = psutil.Process(pid)
            record.update(ppid=child.ppid(), name=child.name(), threads=child.num_threads())
            command = child.cmdline()
            assert 'clover_cli.main' in command and 'owner-confirm' in command, 'never clean an unidentified process'
            child.kill()
            record['owned_cleanup_exit'] = child.wait(timeout=5)
        living.append(record)
    cases = []
    xml = case_out / 'cases.xml'
    if xml.exists():
        for case in ET.parse(xml).getroot().iter('testcase'):
            outcome = next((s for s in ('failure', 'error', 'skipped') if case.find(s) is not None), 'passed')
            cases.append({'classname': case.get('classname'), 'name': case.get('name'), 'outcome': outcome})
    row = {'label': label, 'instrumented_child': observed, 'command': args, 'seconds': time.monotonic()-started, 'exit': exit_code, 'cases': cases, 'process_exit_readback': living}
    save(label + '-result.json', row)
    return row


def main():
    assert os.name == 'nt', 'real native Windows only'
    before = identity('before')
    import getpass
    native_getpass = Path(getpass.__file__).read_bytes()
    (OUT / 'native-getpass.py').write_bytes(native_getpass)
    save('native-getpass-identity.json', {'file': getpass.__file__, 'sha256': hashlib.sha256(native_getpass).hexdigest(), 'selected_function': getpass.getpass.__name__})
    save('dependencies.json', {d.metadata['Name']: d.version for d in importlib.metadata.distributions()})
    save('plan.json', {'kind': 'DIAGNOSTIC_ONLY_NOT_NATIVE_GATE', 'original_failed_run': 38999910756, 'original_fixture': '729e52f38e6f185633f7b60539ce18adb2656a9a', 'orchestration_base': 'd14c37eb2db96081aeee514ff9425f02ce03eb2f', 'cases': ['exact-command-control', 'stdin-identity-experiment', 'wrapped-command-validation'], 'unchanged_cli_timeout': 30, 'workers': 1, 'file_retries': 0, 'instrumentation': 'parent metadata only; child sys.setprofile phase names and 10s repeating faulthandler stacks; no env values/input/output/locals/return values', 'no_production_changes': True, 'not_claimed': ['native gate acceptance', 'console fixture proof', 'install/update proof', 'enrollment', 'release']})
    for path in before['tracked_sha256']:
        file = SOURCE / path
        file.chmod(stat.S_IMODE(file.stat().st_mode) & ~0o222)
    home = OUT / 'synthetic-home'
    guard = home / '.clover'
    guard.mkdir(parents=True)
    (guard / 'pytest_live_guard.py').write_text('"""Offline diagnostic guard; no live permission overrides."""\n')
    (guard / 'sitecustomize.py').write_text('import sys\ndef guard(event,args):\n    if event == "socket.connect":\n        addr=args[1]\n        if isinstance(addr,tuple) and addr[0] not in ("127.0.0.1","::1","localhost"):\n            raise OSError("native owner fixture blocks external network")\n    if event == "socket.getaddrinfo" and args[0] not in (None,"localhost","127.0.0.1","::1"):\n        raise OSError("native owner fixture blocks external DNS")\nsys.addaudithook(guard)\n')
    env = {k: v for k, v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','LOCALAPPDATA','APPDATA','PSMODULEPATH','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432'}}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8='1', TZ='UTC', TERM='xterm-256color')
    probe = subprocess.run([sys.executable, '-c', "import json,sys,importlib.metadata; import gateway.pairing,clover_cli.pairing; print(json.dumps({'python':sys.executable,'pairing_origin':gateway.pairing.__file__,'cli_origin':clover_cli.pairing.__file__,'editable':json.loads(importlib.metadata.distribution('clover-c1').read_text('direct_url.json'))}))"], cwd=OUT, env={**env, 'PYTHONPATH': str(guard)}, capture_output=True, text=True, timeout=20)
    save('preflight.json', {'exit': probe.returncode, 'stdout': probe.stdout, 'stderr': probe.stderr})
    assert probe.returncode == 0
    info = json.loads(probe.stdout)
    assert Path(info['pairing_origin']).resolve() == SOURCE / 'gateway/pairing.py'
    assert Path(info['cli_origin']).resolve() == SOURCE / 'clover_cli/pairing.py'
    bash = shutil.which('bash')
    assert bash and 'git' in bash.lower() and 'wsl' not in bash.lower()
    results = []
    for label, observed in [('exact-command-control', True), ('stdin-identity-experiment', True), ('wrapped-command-validation', False)]:
        results.append(run_case(label, env, bash, observed))
        save('test-results.json', results)
    after = identity('after')
    assert before['tracked_sha256'] == after['tracked_sha256']
    assert all(len(row['cases']) == 10 for row in results), 'incomplete diagnostic file'
    assert all(row['exit'] == 0 for row in results), 'diagnostic observed a failing original path; see retained evidence; NOT native acceptance'


if __name__ == '__main__':
    main()

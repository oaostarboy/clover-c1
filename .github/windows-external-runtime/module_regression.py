"""Native baseline/candidate source replay in one private installed dependency set."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

BASE = '5ec233281f26490471d2e48ee3e5eb0e000d90f8'
TEST = 'tests/clover_cli/test_windows_installer_module_environment.py'


def main():
    source = Path(sys.argv[1]).resolve()
    evidence = Path(os.environ['RUNNER_TEMP']) / 'module-regression'
    evidence.mkdir()
    env = dict(os.environ, CLOVER_HOME=str(evidence / 'home'), PYTHONUTF8='1')
    target = os.environ['TARGET_SHA']
    rows = []
    def command(label, args):
        with (evidence / (label + '.log')).open('wb') as stream:
            p = subprocess.run([str(x) for x in args], cwd=source, env=env,
                               stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=900)
        return p.returncode
    def check(label, args):
        code = command(label, args)
        if code: raise RuntimeError(label + ' exited ' + str(code))
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], env=env, text=True).strip()
    assert git('rev-parse', 'HEAD') == target
    assert git('rev-parse', 'HEAD^{tree}') == os.environ['TARGET_TREE']
    test_bytes = (source / TEST).read_bytes()
    test_hash = hashlib.sha256(test_bytes).hexdigest()
    check('venv', ['uv', 'venv', '.venv', '--python', sys.executable, '--seed'])
    check('dependencies', ['uv', 'sync', '--locked', '--extra', 'all', '--extra', 'dev', '--python', source / '.venv/Scripts/python.exe'])
    bash = Path(os.environ['PROGRAMFILES']) / 'Git/bin/bash.exe'
    assert bash.is_file(), 'Git for Windows Bash required'
    for label, sha in [('baseline', BASE), ('candidate', target)]:
        check(label + '-checkout', ['git', 'checkout', '--detach', sha])
        assert git('rev-parse', 'HEAD') == sha
        assert not git('status', '--porcelain', '--untracked-files=no')
        if label == 'baseline':
            (source / TEST).write_bytes(test_bytes)  # Explicit exact candidate test overlay only.
        assert hashlib.sha256((source / TEST).read_bytes()).hexdigest() == test_hash
        xml = evidence / (label + '.xml')
        code = command(label + '-regression', [bash, 'scripts/run_tests.sh', '-j', '1', '--file-retries', '0', TEST,
                                             '-p', 'no:randomly', '-o', 'addopts=', '-q', '--tb=short', '--junitxml=' + xml.as_posix()])
        cases = [{'id': c.get('classname') + '::' + c.get('name'),
                  'outcome': next((tag for tag in ['failure', 'error', 'skipped'] if c.find(tag) is not None), 'passed')}
                 for c in ET.parse(xml).iter('testcase')]
        rows.append(dict(label=label, sha=sha, tree=git('rev-parse', 'HEAD^{tree}'), returncode=code, cases=cases,
                         module_sha256=hashlib.sha256((source / 'clover_cli/managed_uv.py').read_bytes()).hexdigest(),
                         test_sha256=test_hash))
        (evidence / 'result.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
        assert len(cases) == 4 and len({c['id'] for c in cases}) == 4
        if label == 'baseline':
            assert code != 0 and all(c['outcome'] == 'failure' for c in cases), rows[-1]
            native = next(c for c in ET.parse(xml).iter('testcase') if 'loads_own_security' in c.get('name'))
            assert 'CalledProcessError' in native.find('failure').text, 'Must fail at real native module load'
            (source / TEST).unlink()
        else:
            assert code == 0 and all(c['outcome'] == 'passed' for c in cases), rows[-1]
    assert not git('status', '--porcelain', '--untracked-files=no')
    assert git('rev-parse', 'HEAD') == target
    print(json.dumps(rows))

if __name__ == '__main__': main()

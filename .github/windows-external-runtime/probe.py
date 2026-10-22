"""Disposable hosted Windows external-environment updater probe.

This is NOT Telegram or running-gateway acceptance. It supplies five bounded
native rows missing from the existing checkout-local workflow. Full release
clearance still requires the separate gateway/in-flight-turn gate.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET

CASES = ('existing-upgrade', 'candidate-repair', 'holder-refusal',
         'provision-failure', 'smoke-rollback')


def sha(value):
    if not re.fullmatch('[0-9a-f]{40}', value):
        raise ValueError('An exact lowercase 40-hex source identity is required')
    return value


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_identity(source, expected, evidence, label, commands):
    observed = commands.run(label + '-sha', ['git', '-C', source, 'rev-parse', 'HEAD'])
    if observed != expected:
        raise AssertionError('Installed source SHA differs from declared target')
    if commands.run(label + '-status', ['git', '-C', source, 'status', '--porcelain', '--untracked-files=no']):
        raise AssertionError('Installed tracked source changed')
    if commands.run(label + '-replace', ['git', '-C', source, 'replace', '-l']):
        raise AssertionError('Replacement refs are prohibited')
    hashes = {}
    for rel in ['clover_cli/managed_uv.py', 'clover_cli/update_cmd.py', 'clover_cli/main.py',
                'clover_cli/sqlite_runtime.py', 'clover_cli/gateway.py', 'clover_cli/gateway_windows.py',
                'clover_state.py', 'clover_constants.py',
                'pyproject.toml', 'uv.lock']:
        blob = subprocess.check_output(['git', '-C', str(source), 'show', expected + ':' + rel],
                                       env=commands.env, timeout=30)
        actual = (source / rel).read_bytes()
        if actual != blob:
            raise AssertionError('Installed bytes differ from immutable blob: ' + rel)
        hashes[rel] = hashlib.sha256(actual).hexdigest()
    save(evidence / (label + '-source.json'), {'sha': observed, 'sha256': hashes})


def ensure_state(value, labels):
    if value['integrity'] != [['ok']]:
        raise AssertionError('SQLite integrity_check failed')
    if not set(labels).issubset(value['user_labels']):
        raise AssertionError('transcript state missing')
    if not set(labels).issubset(value['pending']):
        raise AssertionError('pending TodoStore state missing')


def ensure_identity(value, source, live, *, unsafe=None):
    if value['os_name'] != 'nt':
        raise AssertionError('native Windows required, never a simulated OS')
    expected = {'prefix': live, 'selected_runtime': live,
                'selected_updater_runtime': live, 'source': source}
    for key, path in expected.items():
        if Path(value[key]) != Path(path):
            raise AssertionError(f'{key} selected a different install: {value[key]}')
    if Path(value['executable']) != Path(live) / 'Scripts' / 'python.exe':
        raise AssertionError('interpreter moved from its owned external launch location')
    if Path(value['selected_gateway_python']) != Path(live) / 'Scripts' / 'python.exe':
        raise AssertionError('Gateway selector chose a different owned interpreter')
    if unsafe is not None and value['sqlite_unsafe'] is not unsafe:
        raise AssertionError(f'unexpected measured SQLite safety: {value}')


class Commands:
    def __init__(self, evidence, env, cwd):
        self.evidence, self.env, self.cwd = evidence, env, cwd
        self.items = []

    def run(self, label, args, *, expected=0, timeout=2700):
        log = self.evidence / (label + '.log')
        row = {'label': label, 'argv': [str(x) for x in args], 'cwd': str(self.cwd),
               'timeout': timeout, 'log': log.name}
        self.items.append(row)
        save(self.evidence / 'commands.json', self.items)
        with log.open('wb') as stream:
            proc = subprocess.Popen(args, cwd=self.cwd, env=self.env,
                                    stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Only the process launched by this fixture and its descendants.
                # Never a name-based or global process sweep.
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               stdout=stream, stderr=subprocess.STDOUT, check=False)
                proc.wait(timeout=30)
                row.update(timed_out=True, returncode=proc.returncode)
                save(self.evidence / 'commands.json', self.items)
                raise
        row['returncode'] = code
        save(self.evidence / 'commands.json', self.items)
        if expected == 'nonzero':
            if code == 0:
                raise AssertionError(f'{label} unexpectedly succeeded')
        elif code != expected:
            raise AssertionError(f'{label} exited {code}; see {log}')
        return log.read_text(encoding='utf-8', errors='replace').strip()


def clone_exact_source(commands, checkout, source):
    # Windows' autocrlf default must not rewrite bytes before provenance checks.
    # Store this only in the disposable clone so later normal CLI pulls agree.
    commands.run('clone', ['git', 'clone', '--config', 'core.autocrlf=false',
                           '--no-hardlinks', checkout, source])


def execute(a, evidence):
    if os.name != 'nt' or platform.system() != 'Windows':
        raise RuntimeError('This probe executes ONLY on native Windows')
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('An isolated GitHub-hosted runner is required; no personal machines')
    if sys.prefix != sys.base_prefix:
        raise RuntimeError('Driver must not hold the environment it replaces')
    sha(a.target_sha); sha(a.target_tree); sha(a.old_sha)
    root = Path(os.environ['RUNNER_TEMP']) / 'windows-external-runtime'
    if root.exists():
        raise RuntimeError('Refuse reuse of prior test state')
    root.mkdir()
    neutral = root / 'neutral'; neutral.mkdir()
    source = root / 'code'
    live = root / 'owned external environment'
    home = root / 'home'; home.mkdir()
    child = Path(__file__).with_name('child.py').resolve()
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'UV_PROJECT_ENVIRONMENT',
                'UV_PYTHON', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV'):
        env.pop(key, None)
    env.update(CLOVER_HOME=str(home), PYTHONIOENCODING='utf-8', PYTHONUTF8='1',
               GIT_NO_REPLACE_OBJECTS='1')
    c = Commands(evidence, env, neutral)
    save(evidence / 'runner.json', dict(windows=platform.platform(), windows_version=sys.getwindowsversion()[:],
         driver_python=sys.version, executable=sys.executable, run_url=os.environ.get('GITHUB_SERVER_URL','') +
         '/' + os.environ.get('GITHUB_REPOSITORY','') + '/actions/runs/' + os.environ.get('GITHUB_RUN_ID',''),
         job_name=os.environ.get('GITHUB_JOB'), workflow_sha=os.environ.get('GITHUB_SHA'),
         product_sha=a.target_sha, product_tree=a.target_tree, old_sha=a.old_sha,
         helper_hashes={p.name: digest(p) for p in [Path(__file__), child]}))
    checkout = Path(a.source).resolve()
    if c.run('target-sha', ['git', '-C', checkout, 'rev-parse', a.target_sha]) != a.target_sha:
        raise AssertionError('target SHA mismatch')
    if c.run('target-tree', ['git', '-C', checkout, 'rev-parse', a.target_sha + '^{tree}']) != a.target_tree:
        raise AssertionError('target tree mismatch')
    clone_exact_source(c, checkout, source)
    start = a.old_sha if a.case == 'existing-upgrade' else a.target_sha
    c.run('starting-source', ['git', '-C', source, 'checkout', '-B', 'main', start])
    # A new private bare origin has ordinary immutable target objects. No git
    # replace, history rewriting, pushes, or mutation of the published origin.
    origin = root / 'origin.git'
    c.run('origin-init', ['git', 'init', '--bare', origin])
    c.run('origin-target', ['git', '-C', origin, 'fetch', str(checkout),
                             a.target_sha + ':refs/heads/main'])
    c.run('origin-url', ['git', '-C', source, 'remote', 'set-url', 'origin', origin])
    c.run('origin-fetch-config', ['git', '-C', source, 'config', 'remote.origin.fetch',
                                '+refs/heads/main:refs/remotes/origin/main'])
    c.run('fetch', ['git', '-C', source, 'fetch', 'origin'])
    c.run('tracking', ['git', '-C', source, 'branch', '--set-upstream-to=origin/main', 'main'])
    uv = shutil.which('uv')
    if not uv:
        raise RuntimeError('setup-uv did not install uv')
    managed = home / 'bin' / 'uv.exe'; managed.parent.mkdir()
    shutil.copy2(uv, managed)
    c.run('uv-version', [uv, '--version'])
    c.run('venv', [uv, 'venv', live, '--python', sys.executable, '--seed'])
    python = live / 'Scripts' / 'python.exe'
    # Real editable installation, real dependency resolver and wheel metadata.
    c.env.update(UV_PROJECT_ENVIRONMENT=str(live), VIRTUAL_ENV=str(live),
                 UV_PYTHON=str(python), UV_PYTHON_DOWNLOADS='never')
    c.cwd = source
    c.run('installed-dependencies', [uv, 'sync', '--locked', '--extra', 'all', '--python', python])
    c.cwd = neutral
    for key in ('UV_PROJECT_ENVIRONMENT', 'VIRTUAL_ENV', 'UV_PYTHON', 'UV_PYTHON_DOWNLOADS'):
        c.env.pop(key, None)
    # Leave the real catalog refresh policy intact; never mark an old uv fresh.
    def call(label, mode, *extra, **kwargs):
        dest = evidence / (label + '.json')
        c.run(label, [python, '-I', child, mode, '--output', dest, *extra], **kwargs)
        return json.loads(dest.read_text(encoding='utf-8')) if dest.exists() else None
    source_identity(source, start, evidence, 'before', c)
    before = call('before', 'identity')
    # Old source can demonstrate the discovery defect; do not hide or repair it.
    if a.case != 'existing-upgrade':
        ensure_identity(before, source, live, unsafe=True)
    elif not before['sqlite_unsafe']:
        raise AssertionError('Old installed base must really link vulnerable SQLite')
    seeded = call('seed', 'seed'); ensure_state(seeded, ['before-update'])
    if seeded['journal_mode'].lower() == 'wal':
        raise AssertionError('Vulnerable runtime used unsafe WAL')
    holder = None
    try:
        if a.case == 'holder-refusal':
            ready = evidence / 'holder.json'
            holder = subprocess.Popen([python, '-I', child, 'hold', '--output', ready],
                                      cwd=neutral, env=c.env, stdin=subprocess.PIPE)
            deadline = time.monotonic() + 30
            while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if not ready.exists():
                raise RuntimeError('Owned native holder never became ready')
        fault = {'provision-failure': 'provision', 'smoke-rollback': 'post-smoke'}.get(a.case, 'none')
        expected = 'nonzero' if a.case in ('holder-refusal', 'provision-failure', 'smoke-rollback') else 0
        if fault != 'none':
            call('injected-update', 'update', '--fault', fault, expected=expected)
            reached = json.loads((evidence / 'injected-update.json').read_text(encoding='utf-8'))
            if reached['reached'] != fault:
                raise AssertionError('Negative row never reached the named failure boundary')
        else:
            # Same canonical main(), with a transparent observer that rethrows
            # the official installer error unchanged; no repair/guard overrides.
            call('real-cli-update', 'update', expected=expected)
        if holder is not None and holder.poll() is not None:
            raise AssertionError('Updater killed a holder instead of refusing safely')
    finally:
        if holder is not None:
            holder.stdin.close()
            holder.wait(timeout=30)
    labels = ['before-update']
    if a.case == 'smoke-rollback':
        labels.append('new-state-before-rollback')
    after = call('after', 'identity')
    ensure_identity(after, source, live, unsafe=expected != 0)
    ensure_state(call('state-after', 'read'), labels)
    if expected == 0:
        if c.run('landed-sha', ['git', '-C', source, 'rev-parse', 'HEAD']) != a.target_sha:
            raise AssertionError('Normal updater did not land on exact target SHA')
        receipt = home / 'logs' / 'update_receipts' / 'latest.json'
        if not receipt.exists():
            raise AssertionError('Normal updater emitted no completion receipt')
        shutil.copy2(receipt, evidence / 'update-receipt.json')
        if json.loads(receipt.read_text(encoding='utf-8'))['outcome'] != 'success':
            raise AssertionError('CLI exit0 did not mean receipt success')
        call('new-state', 'seed', '--label', 'new-state-after-upgrade')
        backups = list(live.parent.glob(live.name + '.stale.runtime-*'))
        if len(backups) != 1:
            raise AssertionError('Exactly one retained external runtime backup required')
        live.rename(live.with_name(live.name + '.accepted-generation'))
        backups[0].rename(live)
        # Runtime-only rollback intentionally never copies a DB snapshot.
        rolled = call('rollback-runtime', 'identity')
        ensure_identity(rolled, source, live, unsafe=True)
        ensure_state(call('state-after-rollback', 'read'), labels + ['new-state-after-upgrade'])
    elif (after['python'], after['sqlite'], after['base_prefix']) != (before['python'], before['sqlite'], before['base_prefix']):
        raise AssertionError('Failed update changed the old runtime')
    source_identity(source, a.target_sha, evidence, 'final', c)
    for name, path in [('state', home / 'state.db'), ('config', home / 'config.yaml')]:
        if path.exists():
            save(evidence / (name + '-hash.json'), {'sha256': digest(path)})
    return {'case': a.case, 'status': 'PASS', 'scope': 'native CLI/runtime only; no live gateway or Telegram',
            'product_sha': a.target_sha, 'product_tree': a.target_tree}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--target-sha', required=True)
    p.add_argument('--target-tree', required=True)
    p.add_argument('--old-sha', required=True)
    p.add_argument('--case', choices=CASES, required=True)
    p.add_argument('--evidence', required=True)
    a = p.parse_args()
    evidence = Path(a.evidence); evidence.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    suite = ET.Element('testsuite', name='windows-external-runtime', tests='1')
    case = ET.SubElement(suite, 'testcase', name=a.case, classname='native_windows.external_runtime')
    code = 0
    try:
        result = execute(a, evidence)
    except BaseException as exc:
        code = 1
        result = {'case': a.case, 'status': 'FAIL', 'error_type': type(exc).__name__, 'error': str(exc)}
        ET.SubElement(case, 'failure', message=str(exc)).text = traceback.format_exc()
        suite.set('failures', '1')
    finally:
        # Keep the product's redacted diagnostics and receipt on failed rows too.
        # This home exists only inside the disposable GitHub-hosted fixture.
        home = Path(os.environ.get('RUNNER_TEMP', '')) / 'windows-external-runtime' / 'home'
        for relative in ['logs/agent.log', 'logs/errors.log', 'logs/update_receipts/latest.json']:
            source = home / relative
            if source.is_file():
                shutil.copy2(source, evidence / ('product-' + source.name))
    case.set('time', str(time.monotonic() - started))
    save(evidence / 'result.json', result)
    ET.ElementTree(suite).write(evidence / 'native-results.xml', encoding='utf-8', xml_declaration=True)
    print(json.dumps(result))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

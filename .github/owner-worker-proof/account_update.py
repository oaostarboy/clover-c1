"""Startup-only account lifetime adapter; no account/token or product implementation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import time
import tempfile


REQUIRED_HISTORICAL_REF = 'refs/tags/v1.5.6'
# Official GitHub ref read once during this fixture repair. Never replace a
# published tag, accept a similarly named release, or fetch into an install.
READER_PIN: dict = dict(
    ref=REQUIRED_HISTORICAL_REF,
    object='9037790894e70240857a30cbf7a6cd1658834d8a',
    commit='32d3e6728c71d1f7c8684b7b41ac1785cb8117a9',
    tree='a600da1247dd260998dda62f865ff988cdf9eb42',
    reader_blobs={'clover_state.py': dict(
        oid='77933270188e4a8fb3d28c9fd61e774a561c204a',
        sha256='9cf23a0db6ffd60339a0e437a5b1bdfa10bf5477a518ffe4afecadbd4a48f495')})


def historical_reader_identity(repo):
    """Verify the literal approved tag before staging or any account exists."""
    pin = READER_PIN
    if pin['ref'] != REQUIRED_HISTORICAL_REF:
        raise ValueError('Invalid historical reader ref')
    def git(*args):
        return subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args],
                              check=True, capture_output=True).stdout
    try:
        git('check-ref-format', pin['ref'])
        def rev(ref):
            return git('rev-parse', '--verify', ref).decode().strip()
        actual = dict(ref=pin['ref'], object=rev(pin['ref']),
                      commit=rev(pin['ref'] + '^{commit}'), tree=rev(pin['ref'] + '^{tree}'),
                      reader_blobs={name: dict(oid=rev(pin['ref'] + ':' + name),
                          sha256=hashlib.sha256(git('show', pin['ref'] + ':' + name)).hexdigest())
                          for name in pin['reader_blobs']})
    except subprocess.CalledProcessError as exc:
        raise ValueError('Required historical reader history missing') from exc
    if actual != pin:
        raise ValueError('Pinned historical reader identity or proof blob mismatch')
    return actual


def historical_archive_identity(repo, scratch):
    """Persist identity + archive hash before executing the frozen old reader.

    The pinned helper recreates this exact deterministic archive at v156.tar;
    its caller compares that hash after the real read as well.
    """
    identity = historical_reader_identity(repo)
    archive = Path(scratch) / 'v156.tar'
    subprocess.run(['git', '--no-replace-objects', '-C', str(repo), 'archive',
                    '--format=tar', '-o', str(archive), REQUIRED_HISTORICAL_REF],
                   check=True, capture_output=True)
    return dict(identity, archive_sha256=digest(archive))


def source_refs(request):
    return [request[k] for k in ('old', 'candidate', 'target')] + [REQUIRED_HISTORICAL_REF]


def require_complete(repo, refs):
    """Never turn a shallow boundary into an apparently self-contained bundle."""
    def git(*args):
        return subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args],
                              check=True, capture_output=True, text=True).stdout.strip()
    if git('rev-parse', '--is-shallow-repository') != 'false':
        raise ValueError('Refuse shallow packaging input; full checkout required before account creation')
    # Traverse all refs exactly as bundle --all does, including requested history.
    # Missing parents/objects must fail here, not in a newly created account.
    git('rev-list', '--objects', '--all', *refs, '--missing=error')

FIELDS = {'source', 'helpers', 'uv', 'setup', 'old', 'candidate', 'target', 'tree', 'entry', 'turn_mode'}


def validate_request(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('Exact nonsecret Startup request required')
    if any(not isinstance(v, str) for v in value.values()):
        raise ValueError('Request values must be strings')
    if (value['setup'] != 'zyra' or value['entry'] not in {'terminal', 'gateway'}
            or value['turn_mode'] not in {'completed', 'inflight'}):
        raise ValueError('Unsupported Startup case')
    if any(not re.fullmatch('[0-9a-f]{40}', value[k]) for k in ('old', 'candidate', 'target', 'tree')):
        raise ValueError('Literal full source identities required')
    if any(not Path(value[k]).is_absolute() for k in ('source', 'helpers', 'uv')):
        raise ValueError('Absolute input locations required')
    return value


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clone_bundles(inputs, profile, command):
    """The same bundle transport used by the account child and preflight."""
    source, fixture = Path(profile) / 'source', Path(profile) / 'fixture'
    for key, dest in [('source_bundle', source), ('fixture_bundle', fixture)]:
        command(['git', 'clone', '--no-hardlinks', inputs[key], str(dest)])
    return source, fixture


def repo_identity(repo, refs) -> dict:
    def git(*args):
        return subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args],
                              check=True, capture_output=True).stdout
    def rev(ref):
        return git('rev-parse', '--verify', ref).decode().strip()
    files = git('ls-tree', '-rz', 'HEAD')
    blobs = {}
    for item in files.split(b'\0'):
        if not item:
            continue
        metadata, name = item.split(b'\t', 1)
        name = name.decode('utf-8')
        if name.startswith(('.github/owner-worker-proof/', '.github/native-proof/')):
            oid = metadata.decode().split()[2]
            blobs[name] = dict(oid=oid, sha256=hashlib.sha256(git('cat-file', 'blob', oid)).hexdigest())
    return dict(head=rev('HEAD'), tree=rev('HEAD^{tree}'),
                refs={ref: dict(commit=rev(ref + '^{commit}'), tree=rev(ref + '^{tree}')) for ref in refs},
                files_sha256=hashlib.sha256(files).hexdigest(), fixture_blobs=blobs)


def verify_packaging(config, fixture, stage):
    request = config['request']
    refs = source_refs(request)
    reader = historical_reader_identity(request['source'])
    originals = dict(source_bundle=repo_identity(request['source'], refs),
                     fixture_bundle=repo_identity(fixture, ['HEAD']))
    if originals['source_bundle']['refs'][request['target']]['tree'] != request['tree']:
        raise ValueError('Requested target tree differs from source')
    with tempfile.TemporaryDirectory(prefix='clone-control-', dir=stage) as control:
        def command(argv):
            subprocess.run(argv, check=True, capture_output=True)
        # bundle verify alone accepts the demonstrated shallow bundle: clone it.
        for key in originals:
            command(['git', '-C', str(request['source']), 'bundle', 'verify', config[key]])
        source, cloned_fixture = clone_bundles(config, control, command)
        for key, clone, required in [('source_bundle', source, refs),
                                     ('fixture_bundle', cloned_fixture, ['HEAD'])]:
            if key == 'source_bundle' and historical_reader_identity(clone) != reader:
                raise ValueError('Cloned historical reader changed')
            require_complete(clone, required)
            if repo_identity(clone, required) != originals[key]:
                raise ValueError('Bundle clone identity or fixture blob mismatch')
    originals['source_bundle']['historical_reader'] = reader
    return originals


def preflight(request_file, fixture, control_root):
    """Exercise actual staging/clone in a private disposable parent namespace."""
    with tempfile.TemporaryDirectory(prefix='clover-packaging-', dir=control_root) as profile:
        return prepare(request_file, profile, fixture)['packaging']


def prepare(request_file, profile, fixture):
    request = validate_request(json.loads(Path(request_file).read_text(encoding='utf-8-sig')))
    historical_reader_identity(request['source'])
    require_complete(request['source'], source_refs(request))
    require_complete(fixture, ['HEAD'])
    stage = Path(profile) / 'update-inputs'
    stage.mkdir()  # Fresh private account profile only; never overwrite inputs.
    config: dict = dict(request=request, source_refs=source_refs(request))
    for key, repo, refs in [('source_bundle', request['source'],
                             source_refs(request)),
                            ('fixture_bundle', str(fixture), ['HEAD'])]:
        path = stage / (key + '.bundle')
        subprocess.run(['git', '-C', str(repo), 'bundle', 'create', str(path), '--all', *refs],
                       check=True, capture_output=True)
        config[key] = str(path)
        config[key + '_sha256'] = digest(path)
    historical = stage / 'historical'
    historical.mkdir()
    # Pinned proof modules only; never copy runner credentials/config/environment.
    for path in Path(request['helpers']).iterdir():
        if path.is_file() and not path.is_symlink() and path.suffix in {'.py', '.ps1'}:
            shutil.copyfile(path, historical / path.name)
    config['helpers'] = str(historical)
    config['helper_hashes'] = {p.name: digest(p) for p in historical.iterdir()}
    config['uv'] = str(stage / 'uv.exe')
    # Reuse the reviewed setup-uv output resolver, including adjacent .exe.
    resolve = runpy.run_path(str(Path(__file__).with_name('bootstrap.py')))['resolve']
    shutil.copyfile(resolve(request['uv']), config['uv'])
    config['uv_sha256'] = digest(config['uv'])
    config['packaging'] = verify_packaging(config, fixture, stage)
    return config


def driver_command(request, fixture, source, evidence, uv, python):
    argv = [str(python), '-I', str(Path(fixture) / '.github/owner-worker-proof/windows_update.py')]
    values = dict(request, source=str(source), evidence=str(evidence), uv=str(uv))
    del values['helpers']
    for key, value in values.items():
        argv.extend(['--' + key.replace('_', '-'), value])
    return argv


def check_result(result, request):
    expected = dict(status='PASS', target=request['target'], old=request['old'], entry=request['entry'],
                    turn_mode=request['turn_mode'], actual_windows_startup=True, actual_scheduled_task=False,
                    driver_restart=False, updater_owned_successor=True, stability_seconds=90,
                    real_terminal_tool=True, inflight_witness=True, latest_write_state=True)
    if not isinstance(result, dict) or any(type(result.get(k)) is not type(v) or result[k] != v
                                           for k, v in expected.items()):
        raise ValueError('Full Startup acceptance did not complete')


def run(config):
    if (sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'):
        raise RuntimeError('Disposable hosted Windows required')
    request = validate_request(config['update']['request'])
    inputs = config['update']
    profile = Path(config['profile'])
    # Prerequisite evidence is already durable outside the profile. Remove only
    # its exact marker and empty directory; the real installer requires no home.
    home = Path(os.environ['CLOVER_HOME'])
    if home != profile / 'AppData/Local/clover':
        raise ValueError('Account home mismatch')
    marker = home / 'owned-write.txt'
    if marker.read_text() != 'Private non-admin home write control' or set(home.iterdir()) != {marker}:
        raise ValueError('Unexpected pre-install account home')
    marker.unlink()
    home.rmdir()
    for key in ('source_bundle', 'fixture_bundle', 'uv'):
        if digest(inputs[key]) != inputs[key + '_sha256']:
            raise ValueError('Staged input bytes changed')
    for name, expected in inputs['helper_hashes'].items():
        if digest(Path(inputs['helpers']) / name) != expected:
            raise ValueError('Historical proof input changed')
    evidence = Path(config['evidence']) / 'update'
    evidence.mkdir()
    env = dict(os.environ, CLOVER_PROOF_HELPERS=inputs['helpers'],
               CLOVER_FIXTURE_ACCOUNT_SID=config['expected_sid'])
    deadline = time.monotonic() + 4500  # 75 minutes inside the existing 80-minute job.
    with (evidence / 'account-driver.log').open('w') as log:
        def command(argv):
            subprocess.run(list(map(str, argv)), cwd=profile, env=env, check=True,
                           stdout=log, stderr=subprocess.STDOUT, timeout=max(1, min(600, deadline - time.monotonic())))
        source, fixture = clone_bundles(inputs, profile, command)
        tools = profile / 'driver-tools'
        command([inputs['uv'], 'venv', '--python', config['python'], tools])
        python = tools / 'Scripts/python.exe'
        command([inputs['uv'], 'pip', 'install', '--python', python, 'psutil'])
        completed = subprocess.run(driver_command(request, fixture, source, evidence, inputs['uv'], python),
                                   cwd=profile, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   timeout=max(1, deadline - time.monotonic()))
    if completed.returncode != 0:
        raise ValueError('Actual Startup driver failed')
    result = json.loads((evidence / 'result.json').read_text())
    check_result(result, request)
    (Path(config['evidence']) / 'update-result.json').write_text(json.dumps(dict(
        passed=True, sid=config['expected_sid'], profile=str(profile), request=request,
        driver_result=result)), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['preflight', 'prepare', 'run'])
    parser.add_argument('--control-root')
    parser.add_argument('--config')
    parser.add_argument('--request')
    parser.add_argument('--profile')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.mode == 'preflight':
        value = preflight(args.request, Path(__file__).resolve().parents[2], args.control_root)
        if args.output:
            Path(args.output).write_text(json.dumps(value), encoding='utf-8')
        else:
            print(json.dumps(value))
    elif args.mode == 'prepare':
        value = prepare(args.request, args.profile, Path(__file__).resolve().parents[2])
        Path(args.output).write_text(json.dumps(value), encoding='utf-8')
    else:
        run(json.loads(Path(args.config).read_text(encoding='utf-8-sig')))

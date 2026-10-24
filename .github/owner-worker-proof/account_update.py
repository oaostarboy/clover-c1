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


def prepare(request_file, profile, fixture):
    request = validate_request(json.loads(Path(request_file).read_text(encoding='utf-8-sig')))
    stage = Path(profile) / 'update-inputs'
    stage.mkdir()  # Fresh private account profile only; never overwrite inputs.
    config: dict = dict(request=request)
    for key, repo, refs in [('source_bundle', request['source'],
                             [request[k] for k in ('old', 'candidate', 'target')]),
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
        source, fixture = profile / 'source', profile / 'fixture'
        for key, dest in [('source_bundle', source), ('fixture_bundle', fixture)]:
            command(['git', 'clone', '--no-hardlinks', inputs[key], dest])
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
    parser.add_argument('mode', choices=['prepare', 'run'])
    parser.add_argument('--config')
    parser.add_argument('--request')
    parser.add_argument('--profile')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.mode == 'prepare':
        value = prepare(args.request, args.profile, Path(__file__).resolve().parents[2])
        Path(args.output).write_text(json.dumps(value), encoding='utf-8')
    else:
        run(json.loads(Path(args.config).read_text(encoding='utf-8-sig')))

"""Strict wrapper-aware native verifier; no product patch or ancestor exemption."""
from __future__ import annotations
import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def select_candidates(rows, home):
    # Framework argv[0] erases the venv path. Select possible gateways by their
    # actual home and command, then require the EXACT proved two-process topology.
    return {pid for pid, argv, process_home in rows
            if 'gateway' in argv and 'run' in argv and '-c' not in argv
            and process_home == str(home)}


def require_roles(candidates, worker, wrapper, proved):
    if worker == wrapper or candidates != {worker, wrapper} or not proved:
        raise ValueError(f'unproved gateway topology: {sorted(candidates)}; worker={worker}, wrapper={wrapper}')


def observe(home, sha, guard, proof):
    import psutil
    from gateway.status import _get_process_start_time
    state = json.loads((home / 'gateway_state.json').read_text())
    if (state.get('kind') != 'clover-gateway' or not state.get('start_time')
            or Path(state.get('clover_home', '')).resolve() != home.resolve()
            or _get_process_start_time(state['pid']) != state['start_time']):
        raise ValueError('state is not bound to its live profile owner')
    worker = psutil.Process(state['pid'])
    wrapper = psutil.Process(worker.ppid())
    rows = []
    for proc in psutil.process_iter(['cmdline']):
        candidate = False
        try:
            argv = proc.info['cmdline'] or []
            candidate = 'gateway' in argv and 'run' in argv and '-c' not in argv
            if candidate:
                rows.append((proc.pid, argv, proc.environ().get('CLOVER_HOME')))
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            if candidate or proc.pid in (worker.pid, wrapper.pid):
                raise
    proved = guard['_proved_stderr_wrapper'](wrapper.pid, {worker.pid: (home, state['start_time'])})
    require_roles(select_candidates(rows, home), worker.pid, wrapper.pid, proved)
    listeners = sorted({worker.pid for c in worker.net_connections(kind='inet')
                        if c.status == psutil.CONN_LISTEN and c.laddr.port == 18791})
    proof.check_gateway(state, sha, [worker.pid], listeners)
    if (worker.status() == psutil.STATUS_ZOMBIE
            or _get_process_start_time(worker.pid) != state['start_time']
            or worker.ppid() != wrapper.pid):
        raise ValueError('worker identity changed during observation')
    with urllib.request.urlopen('http://127.0.0.1:18791/health', timeout=3) as response:
        if response.status != 200 or json.load(response).get('status') != 'ok':
            raise ValueError('owned gateway health endpoint failed')
    state['native_wrapper'] = dict(pid=wrapper.pid, start_time=_get_process_start_time(wrapper.pid),
                                  argv=wrapper.cmdline(), worker_argv=worker.cmdline())
    return state


def main():
    parser = argparse.ArgumentParser()
    for name in ('source', 'guard-sha', 'home', 'sha', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--seconds', type=int, choices=(0, 90), required=True)
    parser.add_argument('--previous')
    args = parser.parse_args()
    if (sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'):
        parser.error('disposable hosted macOS only')
    source, home = Path(args.source), Path(args.home)
    text = subprocess.check_output(['git', '-C', str(source), 'show',
                                   args.guard_sha + ':clover_cli/update_delivery_guard.py'], text=True)
    names = {'_proved_stderr_wrapper', '_macos_framework_wrapper'}
    nodes = [n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name in names]
    if {n.name for n in nodes} != names:
        raise ValueError('pinned guard seam changed; explicit fixture review required')
    guard = {'Path': Path}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'clover_cli/update_delivery_guard.py', 'exec'), guard)
    spec = importlib.util.spec_from_file_location('native_shared_proof', source / '.github/native-proof/proof.py')
    assert spec is not None and spec.loader is not None
    proof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    previous = json.loads(Path(args.previous).read_text()) if args.previous else None
    deadline = time.monotonic() + 180
    while True:
        try:
            first = observe(home, args.sha, guard, proof)
            if previous and (first['pid'] == previous['pid'] or first['start_time'] == previous['start_time']):
                raise ValueError('gateway has not restarted')
            break
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise
            print(f'waiting: {type(exc).__name__}: {exc}', flush=True)
            time.sleep(2)
    observations = [first]
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        current = observe(home, args.sha, guard, proof)
        if any(current[k] != first[k] for k in ('pid', 'start_time', 'code_sha', 'native_wrapper')):
            raise ValueError('worker/wrapper identity changed in stability window')
        observations.append(current)
        time.sleep(min(3, max(0, end - time.monotonic())))
    Path(args.output).write_text(json.dumps(first, indent=2) + '\n')
    Path(args.output + '.window.json').write_text(json.dumps(observations, indent=2) + '\n')


if __name__ == '__main__':
    main()

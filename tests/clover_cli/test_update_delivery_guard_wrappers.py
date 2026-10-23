"""A proved stderr wrapper is not a second gateway; unknown parents still block."""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def topology(monkeypatch, tmp_path):
    from clover_cli import gateway, update_cmd, update_delivery_guard as guard
    import gateway.status as status
    import psutil

    worker, wrapper = 11413, 11410
    python = str(tmp_path/'external environment/bin/python')
    command = [python, '-m', 'clover_cli.main', 'gateway', 'run', '--external-supervisor']
    wrapper_command = [python, '-m', 'clover_cli.stderr_timestamp', '--error-log',
                       str(tmp_path/'logs/gateway.error.log'), '--', *command]
    starts = {worker: 42, wrapper: 40}
    parents = {worker: wrapper}
    commands = {worker: command, wrapper: wrapper_command}
    row = {'pid': worker, 'kind': 'clover-gateway', 'start_time': 42,
           'clover_home': str(tmp_path), 'update_restart_capability': guard.CAPABILITY}
    path = tmp_path/'gateway_state.json'
    path.write_text(json.dumps(row), encoding='utf-8')
    discovered = [wrapper, worker]
    monkeypatch.setattr(gateway, 'find_profile_gateway_processes', lambda **kw: [
        SimpleNamespace(profile='default', path=tmp_path, pid=worker, create_time=42)])
    monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kw: discovered)
    monkeypatch.setattr(update_cmd, '_gateway_pid_install_verdict', lambda pid: 'unknown')
    monkeypatch.setattr(status, '_pid_exists', lambda pid: True)
    monkeypatch.setattr(status, '_get_process_start_time', lambda pid: starts.get(pid))
    def process(pid):
        if pid not in commands:
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(cmdline=lambda: commands[pid], ppid=lambda: parents.get(pid, 1),
                               environ=lambda: {'XPC_SERVICE_NAME': 'ai.clover.gateway'})
    monkeypatch.setattr(psutil, 'Process', process)
    return SimpleNamespace(guard=guard, starts=starts, parents=parents, commands=commands,
                           worker=worker, wrapper=wrapper, path=path, row=row,
                           discovered=discovered, process=process)


def test_proved_timestamp_wrapper_is_not_a_second_gateway(topology):
    assert topology.guard._unsafe_gateways() == []


def test_proved_historical_launchd_wrapper_matches_its_prepared_child(topology):
    topology.commands[topology.wrapper].remove('--external-supervisor')
    assert topology.guard._unsafe_gateways() == []


@pytest.mark.parametrize('unproved', [
    'gateway-parent', 'unrelated-parent', 'unknown-wrapper-time', 'changed-wrapper-time',
    'unknown-worker-time', 'stale-worker-time', 'missing-capability', 'unknown-capability',
    'wrong-home', 'wrong-log', 'different-child-command', 'not-direct-child',
    'missing-worker', 'unreadable-wrapper', 'unreadable-legacy-environment',
    'missing-legacy-environment', 'malformed-wrapper', 'relative-interpreter',
    'different-interpreter', 'additional-unknown-gateway', 'worker-time-changes-after-admission',
])
def test_wrapper_exception_never_admits_unproved_process(topology, monkeypatch, unproved):
    import psutil
    import gateway.status as status
    t = topology
    if unproved == 'gateway-parent':
        t.commands[t.wrapper] = list(t.commands[t.worker])
    elif unproved == 'unrelated-parent':
        t.commands[t.wrapper] = ['/bin/sh', '-c', 'gateway run']
    elif unproved == 'unknown-wrapper-time':
        t.starts[t.wrapper] = None
    elif unproved == 'changed-wrapper-time':
        calls = []
        def start(pid):
            if pid == t.wrapper:
                calls.append(pid)
                return 40 if len(calls) == 1 else 41
            return t.starts.get(pid)
        monkeypatch.setattr(status, '_get_process_start_time', start)
    elif unproved == 'unknown-worker-time':
        t.starts[t.worker] = None
    elif unproved == 'stale-worker-time':
        t.starts[t.worker] = 41
    elif unproved == 'missing-capability':
        t.row.pop('update_restart_capability')
    elif unproved == 'unknown-capability':
        t.row['update_restart_capability'] = 'future-unknown'
    elif unproved == 'wrong-home':
        t.row['clover_home'] += '/other'
    elif unproved == 'wrong-log':
        t.commands[t.wrapper][4] += '.other'
    elif unproved == 'different-child-command':
        t.commands[t.worker] = [*t.commands[t.worker], '--different']
    elif unproved == 'not-direct-child':
        t.parents[t.worker] = 999
    elif unproved == 'missing-worker':
        t.commands.pop(t.worker)
    elif unproved == 'unreadable-wrapper':
        def process(pid):
            if pid == t.wrapper:
                raise psutil.AccessDenied(pid)
            return t.process(pid)
        monkeypatch.setattr(psutil, 'Process', process)
    elif unproved in ('unreadable-legacy-environment', 'missing-legacy-environment'):
        t.commands[t.wrapper].remove('--external-supervisor')
        def process(pid):
            proc = t.process(pid)
            if pid == t.wrapper:
                if unproved == 'unreadable-legacy-environment':
                    proc.environ = lambda: (_ for _ in ()).throw(psutil.AccessDenied(pid))
                else:
                    proc.environ = lambda: {}
            return proc
        monkeypatch.setattr(psutil, 'Process', process)
    elif unproved == 'malformed-wrapper':
        t.commands[t.wrapper] = []
    elif unproved == 'relative-interpreter':
        t.commands[t.wrapper][0] = 'python'
    elif unproved == 'different-interpreter':
        t.commands[t.wrapper][0] += '-other'
    elif unproved == 'additional-unknown-gateway':
        t.discovered.append(999)
    elif unproved == 'worker-time-changes-after-admission':
        calls = []
        def start(pid):
            if pid == t.worker:
                calls.append(pid)
                return 42 if len(calls) <= 2 else 43
            return t.starts.get(pid)
        monkeypatch.setattr(status, '_get_process_start_time', start)
    t.path.write_text(json.dumps(t.row), encoding='utf-8')
    assert t.guard._unsafe_gateways(), unproved


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize('historical', [False, True])
def test_real_stderr_wrapper_and_direct_worker_identity(monkeypatch, tmp_path, historical):
    """Exercise actual wrapper/psutil topology, not native macOS lifecycle proof."""
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path
    from clover_cli import gateway, update_cmd, update_delivery_guard as guard
    import gateway.status as status
    import psutil

    # A tiny child module keeps the real wrapper's child alive without starting
    # a gateway, transport, or service. Namespace extension loads the real wrapper.
    package = tmp_path/'clover_cli'
    package.mkdir()
    (package/'__init__.py').write_text(
        'from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n', encoding='utf-8')
    marker = tmp_path/'worker.pid'
    (package/'main.py').write_text(
        'import os, time\nfrom pathlib import Path\n'
        'Path(os.environ["REVIEW_WORKER_PID"]).write_text(str(os.getpid()))\n'
        'time.sleep(60)\n', encoding='utf-8')
    child_command = [sys.executable, '-m', 'clover_cli.main', 'gateway', 'run']
    if not historical:
        child_command.append('--external-supervisor')
    command = [sys.executable, '-m', 'clover_cli.stderr_timestamp', '--error-log',
               str(tmp_path/'logs/gateway.error.log'), '--', *child_command]
    env = dict(os.environ, CLOVER_HOME=str(tmp_path/'profile'), REVIEW_WORKER_PID=str(marker),
               XPC_SERVICE_NAME='ai.clover.gateway',
               PYTHONPATH=os.pathsep.join([str(tmp_path), str(Path(guard.__file__).parents[1])]))
    wrapper = subprocess.Popen(command, cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = None
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            assert wrapper.poll() is None, 'real wrapper exited before child was ready'
            time.sleep(0.02)
        assert marker.exists(), 'real wrapper child did not become ready'
        worker = psutil.Process(int(marker.read_text(encoding='utf-8')))
        start = status._get_process_start_time(worker.pid)
        assert start is not None and worker.ppid() == wrapper.pid
        (tmp_path/'gateway_state.json').write_text(json.dumps({
            'pid': worker.pid, 'kind': 'clover-gateway', 'start_time': start,
            'clover_home': str(tmp_path), 'update_restart_capability': guard.CAPABILITY}), encoding='utf-8')
        monkeypatch.setattr(gateway, 'find_profile_gateway_processes', lambda **kw: [
            SimpleNamespace(profile='review', path=tmp_path, pid=worker.pid, create_time=start)])
        monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kw: [wrapper.pid, worker.pid])
        monkeypatch.setattr(update_cmd, '_gateway_pid_install_verdict', lambda pid: 'unknown')
        assert guard._unsafe_gateways() == []
    finally:
        if worker is not None and worker.is_running():
            worker.terminate()
        try:
            wrapper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            wrapper.kill()
            wrapper.wait(timeout=5)

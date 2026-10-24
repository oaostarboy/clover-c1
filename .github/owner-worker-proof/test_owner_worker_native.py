"""Real native child/PID controls; discovery scope only is fixture-selected.

No platform spoofing, fabricated psutil process, signal or termination fallback.
These controls do not certify the installer/update happy path.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='native Windows required')


@pytest.fixture
def real_child():
    import psutil
    child = subprocess.Popen([sys.executable, '-I', '-c',
                              'import sys; sys.stdin.buffer.read()'], stdin=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        launcher = psutil.Process(child.pid)
        while not launcher.children() and time.monotonic() < deadline:
            time.sleep(.05)
        descendants = launcher.children()
        assert len(descendants) == 1, 'venv launcher must have a real base-interpreter child'
        worker = descendants[0]
        assert worker.ppid() == launcher.pid
        assert Path(launcher.exe()).name.lower() == 'python.exe'
        assert launcher.exe().lower() != worker.exe().lower()
        yield launcher, worker
        assert launcher.is_running() and worker.is_running(), 'read-only admission killed a fixture process'
    finally:
        child.communicate(timeout=15)


def test_native_venv_launcher_and_worker_have_distinct_owner_witnesses(real_child, record_property):
    from gateway.status import _get_process_start_time
    from clover_cli.windows_update_receipt_handoff import process_chain
    launcher, worker = real_child
    measured = process_chain(worker.pid, timeout=5)
    assert measured[0]['pid'] == worker.pid
    assert measured[0]['parent_pid'] == launcher.pid
    assert measured[0]['created_unix'] == worker.create_time()
    assert _get_process_start_time(launcher.pid) == int(round(launcher.create_time() * 100))
    assert _get_process_start_time(worker.pid) == int(round(worker.create_time() * 100))
    # Exercise the exact fixture topology function with genuine native images
    # and ancestry, not only portable dictionary controls.
    import importlib.util
    spec = importlib.util.spec_from_file_location('native_fixture_topology', Path(__file__).with_name('topology.py'))
    assert spec is not None and spec.loader is not None
    topology = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(topology)
    members = topology.pair([dict(pid=q.pid, ppid=q.ppid(), exe=q.exe(), argv=q.cmdline())
                             for q in [launcher, worker]],
                            sys.executable, str(Path(sys._base_executable).resolve(strict=True)))
    assert [q['pid'] for q in members] == [launcher.pid, worker.pid]
    record_property('launcher_pid', launcher.pid)
    record_property('worker_pid', worker.pid)
    record_property('worker_birth', worker.create_time())


@pytest.mark.parametrize('reporter_count', [1, 4])
def test_real_reporter_preserves_live_owner_identity_and_restart_contract(tmp_path, monkeypatch, record_property, reporter_count):
    home = tmp_path / 'owner-reporter-home'
    home.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(home))
    env = dict(os.environ, CLOVER_HOME=str(home))
    owner_code = (
        'import json,os,sys; from gateway import status; '
        'status.write_pid_file(); '
        'status.write_runtime_status(gateway_state="running", active_agents=0, '
        'restart_requested=False, update_restart_capability="reply-carriers-through-ack-v1"); '
        'print(json.dumps({"worker":os.getpid()}), flush=True); sys.stdin.buffer.read()'
    )
    owner = subprocess.Popen([sys.executable, '-I', '-c', owner_code], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert owner.stdout is not None
        ready = json.loads(owner.stdout.readline())
        path = home / 'gateway_state.json'
        before = json.loads(path.read_text())
        assert before['pid'] == ready['worker']
        reporters = [subprocess.Popen([sys.executable, '-I', '-c',
            'from gateway import status; status.write_runtime_status('
            f'platform="native-reporter-{index}",platform_state="connected")'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for index in range(reporter_count)]
        for reporter in reporters:
            _, error = reporter.communicate(timeout=30)
            assert reporter.returncode == 0, error
        after = json.loads(path.read_text())
        record_property('owner_worker_pid', ready['worker'])
        record_property('top_level_pid_after_reporter', after['pid'])
        for field in ['pid', 'start_time', 'kind', 'clover_home', 'argv', 'code_sha', 'update_restart_capability']:
            assert after.get(field) == before.get(field), (field, before, after)
        for index in range(reporter_count):
            entry = after['platforms'][f'native-reporter-{index}']
            assert entry['state'] == 'connected'
            assert entry['writer_pid'] != ready['worker']
        assert owner.poll() is None
    finally:
        owner.communicate(timeout=15)


@pytest.mark.parametrize('condition', ['missing', 'invalid-json', 'reused-pid', 'foreign-home'])
def test_untrusted_status_cannot_admit_a_real_live_process(real_child, tmp_path, monkeypatch, condition):
    from clover_cli import gateway, update_delivery_guard as guard
    from gateway.status import _get_process_start_time
    launcher, worker = real_child
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(home))
    row = dict(pid=worker.pid, kind='clover-gateway', clover_home=str(home),
               start_time=_get_process_start_time(worker.pid), active_agents=0,
               gateway_state='running', restart_requested=False,
               update_restart_capability=guard.CAPABILITY)
    path = home / 'gateway_state.json'
    if condition == 'invalid-json':
        path.write_text('{not json', encoding='utf-8')
    elif condition != 'missing':
        if condition == 'reused-pid':
            row['start_time'] += 100
        else:
            row['clover_home'] = str(tmp_path / 'foreign')
        path.write_text(json.dumps(row), encoding='utf-8')
    # Only constrain discovery to this disposable case. All PID/birth/image,
    # ancestry, status parsing and product admission logic remain real.
    mapped = gateway.ProfileGatewayProcess(profile='proof', path=home,
        pid=worker.pid, create_time=worker.create_time())
    monkeypatch.setattr(gateway, 'find_profile_gateway_processes', lambda **kwargs: [mapped])
    monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kwargs: [launcher.pid, worker.pid])
    before = path.read_bytes() if path.exists() else None
    reasons = guard._unsafe_gateways()
    assert any(not reason.startswith('legacy: ') for reason in reasons), reasons
    assert (path.read_bytes() if path.exists() else None) == before
    assert launcher.is_running() and worker.is_running()

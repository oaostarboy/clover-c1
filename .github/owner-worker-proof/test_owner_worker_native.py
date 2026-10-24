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
    record_property('launcher_pid', launcher.pid)
    record_property('worker_pid', worker.pid)
    record_property('worker_birth', worker.create_time())


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

"""Native Windows child birth/CIM/beacon proof; never touches a real install.

The inverted-clock row controls only the producer wall clock. Child PID, birth,
parent edge and executable come from actual Windows processes, not fixture rows.
"""
from datetime import datetime
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from clover_cli import update_restart_watcher as watcher
from clover_cli import windows_update_receipt_handoff as bridge

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows process proof')


@pytest.mark.parametrize('invert_clock', [False, True])
def test_measured_windows_child_beacon_keeps_strict_birth_boundary(tmp_path, monkeypatch, record_property, invert_clock):
    home = tmp_path / 'disposable-home'
    directory = home / 'logs/update_receipts'
    directory.mkdir(parents=True)
    repo = home / 'clover-c1'
    watcher.write_beacon([], clover_home=home, repo=str(repo), pre_pull_sha='a' * 40)
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        birth = watcher._process_create_time(child.pid)
        assert birth is not None
        if invert_clock:
            monkeypatch.setattr(watcher, 'time', SimpleNamespace(time=lambda: birth - .000468))
        assert watcher.hand_off_beacon(child.pid, clover_home=home)
        began = time.monotonic()
        chain = bridge.process_chain(child.pid, timeout=5)
        elapsed = time.monotonic() - began
        measured = chain[0]
        assert measured['pid'] == child.pid
        assert measured['parent_pid'] > 0
        created = datetime.fromisoformat(measured['created_utc']).timestamp()
        assert abs(created - birth) <= .001
        beacon_path = home / watcher.BEACON_NAME
        beacon = json.loads(beacon_path.read_text())
        assert beacon['refreshed_at'] >= created
        owned = [{**measured, 'created': created}]
        parent = {'pre_update': {'sha': 'a' * 40}, 'pre_update_snapshot_id': None}
        assert bridge._beacon(directory, parent, owned, repo, time.time)
        for fault in ['missing_birth', 'reused_pid', 'wrong_source', 'stale_refresh']:
            invalid = dict(beacon)
            if fault == 'missing_birth':
                invalid['updater_create_time'] = None
            elif fault == 'reused_pid':
                invalid['updater_create_time'] = birth - 10
            elif fault == 'wrong_source':
                invalid['pre_pull_sha'] = 'c' * 40
            else:
                invalid['refreshed_at'] = created - .000468
            beacon_path.write_text(json.dumps(invalid))
            with pytest.raises(bridge.Unverified):
                bridge._beacon(directory, parent, owned, repo, time.time)
        record_property('actual_pid', child.pid)
        record_property('actual_birth', birth)
        record_property('cim_created_utc', measured['created_utc'])
        record_property('producer_refreshed_at', beacon['refreshed_at'])
        record_property('controlled_inversion', invert_clock)
        record_property('process_query_elapsed_seconds', elapsed)
    finally:
        child.communicate(timeout=10)

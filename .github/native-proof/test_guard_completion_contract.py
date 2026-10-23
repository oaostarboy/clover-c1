"""Bounded completion observer preserves real update exits and receipt identity."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HELPERS = Path(__file__).resolve().parent
sys.path.insert(0, str(HELPERS))
spec = importlib.util.spec_from_file_location('native_guard_completion_contract', HELPERS / 'guard_native_upgrade.py')
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_handler_observes_real_marker_after_old_180_second_cutoff(tmp_path, monkeypatch):
    clock = [0.0]
    marker = tmp_path / '.update_exit_code'
    def sleep(delay):
        clock[0] += delay
        if clock[0] >= 240:
            marker.write_text('0')
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    assert module.wait_update_exit(tmp_path, None, tmp_path) == 0
    assert 240 <= clock[0] <= 1800
    assert json.loads((tmp_path / 'gateway-update-exit.json').read_text())['exit'] == 0


def test_handler_never_turns_real_failure_marker_into_success(tmp_path):
    (tmp_path / '.update_exit_code').write_text('7')
    assert module.wait_update_exit(tmp_path, None, tmp_path) == 7
    assert json.loads((tmp_path / 'gateway-update-exit.json').read_text())['exit'] == 7


def test_absent_marker_remains_a_failed_observation(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0],
        sleep=lambda delay: clock.__setitem__(0, clock[0] + delay)))
    with pytest.raises(RuntimeError, match='bounded observation expired'):
        module.wait_update_exit(tmp_path, None, tmp_path, seconds=4)
    assert not (tmp_path / 'gateway-update-exit.json').exists()


def test_receipt_observer_waits_for_logical_child_and_retains_failure(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    receipt_dir = tmp_path / 'logs/update_receipts'
    receipt_dir.mkdir(parents=True)
    path = receipt_dir / 'latest.json'
    running = {'outcome': 'running', 'finished_at': None}
    path.write_text(json.dumps(running))
    final = {'outcome': 'success', 'started_at': datetime.fromtimestamp(1, timezone.utc).isoformat(),
             'finished_at': datetime.fromtimestamp(2, timezone.utc).isoformat(),
             'pre_update': {'sha': 'a' * 40}, 'post_update': {'sha': 'b' * 40}}
    clock = [0.0]
    def sleep(delay):
        clock[0] += delay
        if clock[0] >= 3:
            path.write_text(json.dumps(final))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    assert module.wait_update_receipt(tmp_path, tmp_path, 0, 'a' * 40, 'b' * 40) == final
    assert clock[0] == 3
    final['outcome'] = 'failed'
    path.write_text(json.dumps(final))
    with pytest.raises(ValueError, match='receipt'):
        module.wait_update_receipt(tmp_path, tmp_path, 0, 'a' * 40, 'b' * 40)
    assert json.loads((tmp_path / 'update-receipt.json').read_text())['outcome'] == 'failed'


def test_legacy_boundary_requires_old_source_at_actual_original_death():
    death = {'state': {'pid': 42, 'code_sha': 'a' * 40}, 'pid_alive': False, 'disk_sha': 'a' * 40}
    assert module.legacy_stop_witness([death], 42, 'a' * 40) == death
    for changed in [
        {**death, 'disk_sha': 'b' * 40},
        {**death, 'pid_alive': True},
        {**death, 'state': {'pid': 99, 'code_sha': 'a' * 40}},
        {**death, 'state': {'pid': 42, 'code_sha': 'b' * 40}},
    ]:
        with pytest.raises(AssertionError):
            module.legacy_stop_witness([changed], 42, 'a' * 40)


def test_terminal_status_remains_the_actual_process_result(tmp_path):
    calls = []
    update = SimpleNamespace(pid=321, wait=lambda timeout: calls.append(timeout) or 9)
    assert module.wait_update_exit(tmp_path, update, tmp_path) == 9
    assert calls == [1800]
    assert json.loads((tmp_path / 'terminal-update-exit.json').read_text()) == {'exit': 9, 'pid': 321}

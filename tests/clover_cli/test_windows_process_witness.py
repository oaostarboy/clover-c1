"""Receipt ancestry reader must not depend on PowerShell/CIM startup."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from clover_cli import windows_update_receipt_handoff as bridge


def test_process_chain_uses_isolated_base_python_without_importing_native_modules(monkeypatch):
    calls = []
    row = {'pid': 123, 'parent_pid': 122, 'executable': r'C:\\python.exe',
           'created_utc': '2026-10-23T03:00:00+00:00'}
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=json.dumps([row]))
    monkeypatch.setattr(bridge.subprocess, 'run', run)
    before = set(sys.modules)
    assert bridge.process_chain(123, timeout=4) == [row]
    argv, options = calls[0]
    assert Path(argv[0]).resolve() == Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    assert argv[1] == '-I'
    assert Path(argv[2]).name == 'windows_process_witness.py'
    assert argv[3:] == ['123']
    assert options['timeout'] == 4 and options['check'] is True
    assert not ({'psutil', '_ctypes'} - before) & set(sys.modules)


@pytest.mark.parametrize('pid', [True, 0, -1, '123', 2**40])
def test_process_chain_refuses_invalid_pid_without_spawning(monkeypatch, pid):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('invalid identity reached process creation')
    monkeypatch.setattr(bridge.subprocess, 'run', forbidden)
    with pytest.raises(bridge.Unverified):
        bridge.process_chain(pid)


def test_filetime_conversion_preserves_native_submicrosecond_precision():
    from clover_cli.windows_process_witness import filetime_to_unix
    # A legal 100ns FILETIME vector; subtracting after float conversion loses
    # precision compared with the native machine measurement.
    assert filetime_to_unix(134372017347630808) == 1792728134.7630808


def test_chain_uses_measured_rows_and_stops_only_at_absent_ancestor():
    from clover_cli.windows_process_witness import chain_from_snapshot
    observations = []
    def observe(pid):
        observations.append(pid)
        return {'executable': f'C:\\python-{pid}.exe', 'created_utc': '2026-10-23T03:00:00+00:00'}
    rows = chain_from_snapshot(123, {123: 122, 122: 121}, observe)
    assert observations == [123, 122]
    assert [row['pid'] for row in rows] == [123, 122]
    assert [row['parent_pid'] for row in rows] == [122, 121]
    assert rows[0]['executable'] == 'C:\\python-123.exe'
    with pytest.raises(ValueError, match='missing current'):
        chain_from_snapshot(999, {123: 122}, observe)
    with pytest.raises(ValueError, match='cyclic'):
        chain_from_snapshot(123, {123: 122, 122: 123}, observe)
    def denied(pid):
        raise PermissionError('unobservable process')
    with pytest.raises(PermissionError):
        chain_from_snapshot(123, {123: 122}, denied)


def test_native_query_errors_do_not_produce_a_synthetic_witness(monkeypatch):
    import subprocess
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('private-native-command', 4)
    monkeypatch.setattr(bridge.subprocess, 'run', timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        bridge.process_chain(123, timeout=4)

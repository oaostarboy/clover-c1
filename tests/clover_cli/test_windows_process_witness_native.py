"""Actual Win32 image/birth/ancestry evidence; disposable native CI only."""
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from clover_cli import windows_update_receipt_handoff as bridge

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Native Win32 process evidence')


def test_real_current_process_witness_matches_independent_psutil(record_property):
    import psutil
    before = set(sys.modules)
    began = time.monotonic()
    chain = bridge.process_chain(os.getpid(), timeout=5)
    elapsed = time.monotonic() - began
    current = psutil.Process()
    measured = chain[0]
    assert measured['pid'] == current.pid
    assert measured['parent_pid'] == current.ppid()
    assert Path(measured['executable']).resolve() == Path(current.exe()).resolve()
    assert abs(datetime.fromisoformat(measured['created_utc']).timestamp() - current.create_time()) <= .001
    assert measured['created_unix'] == current.create_time()
    assert bridge._birth(measured) == current.create_time()
    assert not ({'_ctypes'} - before) & set(sys.modules)
    record_property('native_query_seconds', elapsed)
    record_property('actual_pid', current.pid)
    record_property('actual_birth', current.create_time())


def test_real_live_child_has_exact_parent_and_creation_time(record_property):
    import psutil
    child = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-I', '-c',
                              'import sys; sys.stdin.buffer.read()'], stdin=subprocess.PIPE)
    try:
        actual = psutil.Process(child.pid)
        chain = bridge.process_chain(child.pid, timeout=5)
        assert chain[0]['pid'] == child.pid
        assert chain[0]['parent_pid'] == os.getpid()
        assert Path(chain[0]['executable']).resolve() == Path(actual.exe()).resolve()
        assert abs(datetime.fromisoformat(chain[0]['created_utc']).timestamp() - actual.create_time()) <= .001
        assert chain[0]['created_unix'] == actual.create_time()
        assert bridge._birth(chain[0]) == actual.create_time()
        record_property('actual_child_pid', child.pid)
        record_property('actual_child_birth', actual.create_time())
    finally:
        child.communicate(timeout=10)


def test_missing_native_process_cannot_supply_an_ownership_witness():
    import psutil
    # psutil's Windows pid_exists accepts signed C long, unlike Win32 DWORD.
    # Keep the missing-process control inside both APIs' supported range.
    pid = 0x7FFFFFFC
    assert not psutil.pid_exists(pid)
    with pytest.raises(subprocess.CalledProcessError):
        bridge.process_chain(pid, timeout=5)

"""Real Windows descriptor/byte-range controls for status-lock correction27f507.

These tests do NOT prove cross-user Windows ACL isolation. The Clover home must
already have appropriate inherited ACLs. No test changes ACLs or token privileges.
Missing native link capability is a failed/unexercised gate, never a silent pass.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='native Windows required')


@pytest.fixture
def home(tmp_path, monkeypatch, record_property):
    path = tmp_path / 'native-status-lock-home'
    path.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(path))
    record_property('acl_isolation', 'NOT EXERCISED; inherited-home ACL is a prerequisite')
    record_property('native_lock', 'Windows msvcrt byte-range lock; no platform monkeypatch')
    return path


@pytest.mark.parametrize('preexisting', [False, True])
def test_native_fdopen_rplus_locks_one_stable_file(home, preexisting):
    from gateway import status
    lock = home / 'gateway_state.lock'
    original = b'existing lock data\n'
    if preexisting:
        lock.write_bytes(original)
    before = lock.stat() if lock.exists() else None
    with status._open_runtime_status_lock(lock) as handle:
        assert handle.mode == 'r+'
        assert handle.readable() and handle.writable()
        assert os.path.samestat(os.fstat(handle.fileno()), lock.stat())
        assert status._try_acquire_file_lock(handle)
        try:
            assert os.path.samestat(os.fstat(handle.fileno()), lock.stat())
        finally:
            status._release_file_lock(handle)
    if before is not None:
        assert os.path.samestat(before, lock.stat())
        assert lock.read_bytes() == original
    else:
        assert lock.read_bytes() == b'\r\n' or lock.read_bytes() == b'\n'
    status.write_runtime_status(active_agents=7)
    assert json.loads((home / 'gateway_state.json').read_text())['active_agents'] == 7


def test_real_other_process_cannot_enter_held_native_byte_range(home, record_property):
    from gateway import status
    status.write_runtime_status(active_agents=0)
    lock = home / 'gateway_state.lock'
    before = lock.stat()
    env = dict(os.environ, CLOVER_HOME=str(home))
    with status._open_runtime_status_lock(lock) as held:
        assert status._try_acquire_file_lock(held)
        try:
            code = '''import json,time
from gateway import status
began=time.monotonic()
try:
    status.write_runtime_status(active_agents=13)
except TimeoutError as exc:
    print(json.dumps(dict(blocked=True, detail=str(exc), elapsed=time.monotonic()-began)))
else:
    raise AssertionError('entered a byte range held by another process')
'''
            child = subprocess.run([sys.executable, '-I', '-c', code], env=env,
                                   capture_output=True, text=True, timeout=10)
            assert child.returncode == 0, child.stderr
            observed = json.loads(child.stdout)
            assert observed['blocked'] is True
            assert 0.8 <= observed['elapsed'] < 3
            assert json.loads((home / 'gateway_state.json').read_text())['active_agents'] == 0
            assert os.path.samestat(before, lock.stat())
            record_property('contender_wait_seconds', observed['elapsed'])
        finally:
            status._release_file_lock(held)
    # Release, not a retry/force-stop of the holder, makes the same inode usable.
    child = subprocess.run([sys.executable, '-I', '-c',
        'from gateway import status; status.write_runtime_status(active_agents=13)'],
        env=env, capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    assert os.path.samestat(before, lock.stat())
    assert json.loads((home / 'gateway_state.json').read_text())['active_agents'] == 13


def test_native_process_exit_releases_same_lock_inode(home):
    from gateway import status
    status.write_runtime_status(active_agents=0)
    lock = home / 'gateway_state.lock'
    before = lock.stat()
    code = '''import os
from pathlib import Path
from gateway import status
h=status._open_runtime_status_lock(Path(os.environ['CLOVER_HOME'])/'gateway_state.lock')
assert h.mode == 'r+'
assert status._try_acquire_file_lock(h)
os._exit(0)
'''
    child = subprocess.run([sys.executable, '-I', '-c', code],
        env=dict(os.environ, CLOVER_HOME=str(home)), capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    status.write_runtime_status(active_agents=13)
    assert os.path.samestat(before, lock.stat())
    assert json.loads((home / 'gateway_state.json').read_text())['active_agents'] == 13


def test_native_hardlink_lock_is_rejected_without_touching_target(home):
    from gateway import status
    target = home / 'private-target'
    target.write_bytes(b'not a lock; retain every byte\n')
    lock = home / 'gateway_state.lock'
    os.link(target, lock)
    before = target.stat()
    assert before.st_nlink == 2
    with pytest.raises(OSError, match='single regular file'):
        status.write_runtime_status(active_agents=13)
    assert target.read_bytes() == b'not a lock; retain every byte\n'
    assert os.path.samestat(before, target.stat())
    assert lock.stat().st_nlink == 2
    assert not (home / 'gateway_state.json').exists()


def test_native_file_reparse_lock_is_rejected_without_touching_target(home, record_property):
    from gateway import status
    target = home / 'private-target'
    target.write_bytes(b'unchanged target\n')
    lock = home / 'gateway_state.lock'
    # Do not enable Developer Mode, add privileges or alter security policy.
    # A hosted runner that cannot create this object cannot certify this case.
    os.symlink(target, lock)
    metadata = lock.lstat()
    assert metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    with pytest.raises(OSError):
        status.write_runtime_status(active_agents=13)
    assert lock.is_symlink()
    assert target.read_bytes() == b'unchanged target\n'
    assert not (home / 'gateway_state.json').exists()
    record_property('native_object', 'real file symlink/reparse point')


def test_native_directory_lock_is_rejected_without_status_publish(home):
    from gateway import status
    lock = home / 'gateway_state.lock'
    lock.mkdir()
    retained = lock / 'retained'
    retained.write_bytes(b'keep\n')
    with pytest.raises(OSError):
        status.write_runtime_status(active_agents=13)
    assert retained.read_bytes() == b'keep\n'
    assert not (home / 'gateway_state.json').exists()

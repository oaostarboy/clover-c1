"""POSIX checkpoint barriers use real files and instrumented real syscalls."""
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

import agent.storage_recovery as recovery
from tests.run_agent.test_storage_checkpoint_lifetime import blocked_result

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX directory durability contract')


@pytest.mark.parametrize('existing', [False, True])
def test_parent_barrier_precedes_checkpoint_publication(tmp_path, existing):
    directory = tmp_path / 'storage-recovery'
    if existing:
        directory.mkdir(mode=0o700)
    target = directory / 'result.json'
    opened, events = {}, []
    real_open, real_sync, real_replace = os.open, os.fsync, os.replace
    def track_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        opened[fd] = str(path)
        return fd
    def track_sync(fd):
        events.append(('sync', opened.get(fd)))
        return real_sync(fd)
    def track_replace(*args, **kwargs):
        events.append(('publish', None))
        return real_replace(*args, **kwargs)
    with (patch.object(os, 'open', track_open), patch.object(os, 'fsync', track_sync),
          patch.object(os, 'replace', track_replace)):
        recovery._write_checkpoint(target, {'safe': True})
    assert ('sync', str(tmp_path)) in events
    assert events.index(('sync', str(tmp_path))) < events.index(('publish', None))
    assert events[-1] == ('sync', str(directory))
    assert json.loads(target.read_text()) == {'safe': True}
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize('existing', [False, True])
def test_failed_parent_barrier_prevents_checkpoint_publication(tmp_path, existing):
    directory = tmp_path / 'storage-recovery'
    if existing:
        directory.mkdir(mode=0o700)
    target = directory / 'result.json'
    opened = {}
    real_open, real_sync = os.open, os.fsync
    def track_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        opened[fd] = str(path)
        return fd
    def fail_parent(fd):
        if opened.get(fd) == str(tmp_path):
            raise OSError('synthetic parent fsync failure')
        return real_sync(fd)
    with patch.object(os, 'open', track_open), patch.object(os, 'fsync', fail_parent):
        with pytest.raises(OSError, match='parent fsync failure'):
            recovery._write_checkpoint(target, {'safe': True})
    assert not target.exists()
    assert not list(directory.glob('.pending-*'))
    # A directory left by a failed/crashed first creator still needs its barrier.
    recovery._write_checkpoint(target, {'safe': True})
    assert json.loads(target.read_text()) == {'safe': True}


def test_parent_barrier_failure_does_not_authorize_storage_retry(blocked_result, tmp_path):
    agent, db, history, release = blocked_result
    opened = {}
    real_open, real_sync = os.open, os.fsync
    def track_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        opened[fd] = str(path)
        return fd
    def fail_parent(fd):
        if opened.get(fd) == str(tmp_path):
            raise OSError('synthetic parent fsync failure')
        return real_sync(fd)
    with patch.object(os, 'open', track_open), patch.object(os, 'fsync', fail_parent):
        assert agent._flush_messages_to_session_db(history) is False
    assert release.seen
    assert agent._storage_recovery_checkpoint is None
    assert not any(m['role'] == 'tool' for m in db.get_messages_as_conversation('worker'))
    assert not list((tmp_path / 'storage-recovery').glob('*.json'))


@pytest.mark.parametrize('point', ['parent-open', 'publish'])
def test_directory_replacement_never_leaks_checkpoint_or_reports_success(tmp_path, point):
    directory = tmp_path / 'storage-recovery'
    directory.mkdir(mode=0o700)
    moved = tmp_path / 'moved-original'
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o700)
    target = directory / 'result.json'
    real_open, real_replace = os.open, os.replace
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            directory.rename(moved)
            directory.symlink_to(outside, target_is_directory=True)
    def racing_open(path, flags, *args, **kwargs):
        if point == 'parent-open' and Path(path) == tmp_path:
            swap()
        return real_open(path, flags, *args, **kwargs)
    def racing_replace(*args, **kwargs):
        if point == 'publish':
            swap()
        return real_replace(*args, **kwargs)
    with patch.object(os, 'open', racing_open), patch.object(os, 'replace', racing_replace):
        with pytest.raises(OSError):
            recovery._write_checkpoint(target, {'private': 'SYNTHETIC sensitive result'})
    assert swapped
    assert list(outside.iterdir()) == []
    assert not list(moved.glob('.pending-*'))


def test_canonical_replace_failure_keeps_pending_payload(blocked_result):
    agent, db, history, release = blocked_result
    real_replace = os.replace
    calls = 0
    def fail_canonical(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('synthetic canonical replace failure')
        return real_replace(*args, **kwargs)
    with patch.object(os, 'replace', fail_canonical):
        assert agent._flush_messages_to_session_db(history) is True
    path = Path(agent._storage_recovery_checkpoint)
    pending = json.loads(path.read_text())
    assert pending['disposition'] == 'pending'
    assert pending['messages'][-1]['content'] == history[-1]['content']
    assert not list(path.parent.glob('.pending-*'))
    assert sum(m['role'] == 'tool' for m in db.get_messages_as_conversation('worker')) == 1


def test_temporary_name_collision_preserves_unowned_file(tmp_path):
    from types import SimpleNamespace
    directory = tmp_path / 'storage-recovery'
    directory.mkdir(mode=0o700)
    collision = directory / '.pending-collision'
    collision.write_text('unrelated pending evidence')
    collision.chmod(0o600)
    with patch.object(recovery.uuid, 'uuid4', return_value=SimpleNamespace(hex='collision')):
        with pytest.raises(FileExistsError):
            recovery._write_checkpoint(directory / 'result.json', {'safe': True})
    assert collision.read_text() == 'unrelated pending evidence'
    assert not (directory / 'result.json').exists()



"""Real NTFS handles, not mocked syscall behavior. Required native proof lane."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent import windows_audit_reader as reader
from agent.work_lifecycle import _audit_bytes

pytestmark = pytest.mark.windows_only


def test_real_bounded_regular_read(tmp_path):
    (tmp_path / 'dir').mkdir()
    data = b'actual bytes\x00\xff'
    (tmp_path / 'dir' / 'file').write_bytes(data)
    assert _audit_bytes(str(tmp_path), 'dir/file', len(data)) == data
    assert reader.read_bytes(str(tmp_path), 'dir/file', len(data)) == data
    with pytest.raises(ValueError, match='size bound'):
        _audit_bytes(str(tmp_path), 'dir/file', len(data) - 1)
    (tmp_path / 'empty').write_bytes(b'')
    assert _audit_bytes(str(tmp_path), 'empty', 0) == b''


@pytest.mark.parametrize('where', ['root', 'ancestor', 'leaf'])
def test_junctions_are_rejected_at_every_level(tmp_path, where):
    real = tmp_path / 'real'
    real.mkdir()
    (real / 'file').write_bytes(b'outside')
    link = tmp_path / 'link'
    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(real)],
                   check=True, capture_output=True)
    try:
        project, name = {
            'root': (str(link), 'file'),
            'ancestor': (str(tmp_path), 'link/file'),
            'leaf': (str(tmp_path), 'link'),
        }[where]
        with pytest.raises((OSError, ValueError)):
            _audit_bytes(project, name, 100)
    finally:
        os.rmdir(link)


def test_file_symlink_is_rejected(tmp_path):
    (tmp_path / 'real').write_bytes(b'outside')
    link = tmp_path / 'link'
    os.symlink(tmp_path / 'real', link)
    with pytest.raises((OSError, ValueError)):
        _audit_bytes(str(tmp_path), 'link', 100)


def test_directory_is_not_a_regular_file(tmp_path):
    (tmp_path / 'dir').mkdir()
    with pytest.raises((OSError, ValueError)):
        _audit_bytes(str(tmp_path), 'dir', 100)


def test_existing_writer_refuses_read(tmp_path):
    path = tmp_path / 'file'
    path.write_bytes(b'original')
    with path.open('r+b'):
        with pytest.raises(OSError):
            _audit_bytes(str(tmp_path), 'file', 100)


def test_leaf_write_and_delete_are_locked_during_read(tmp_path, monkeypatch):
    path = tmp_path / 'file'
    path.write_bytes(b'original')
    api = reader._api()
    original = api.read
    observed = []

    def during(handle, size):
        with pytest.raises(OSError):
            path.write_bytes(b'mutated!')
        with pytest.raises(OSError):
            path.unlink()
        observed.append(True)
        return original(handle, size)

    api.read = during
    monkeypatch.setattr(reader, '_api', lambda: api)
    assert _audit_bytes(str(tmp_path), 'file', 100) == b'original'
    assert observed == [True]
    # Successful cleanup also proves handles close after return.
    path.unlink()


def test_ancestor_replacement_is_locked_during_read(tmp_path, monkeypatch):
    directory = tmp_path / 'dir'
    directory.mkdir()
    (directory / 'file').write_bytes(b'original')
    api = reader._api()
    original = api.read
    observed = []

    def during(handle, size):
        with pytest.raises(OSError):
            directory.rename(tmp_path / 'moved')
        observed.append(True)
        return original(handle, size)

    api.read = during
    monkeypatch.setattr(reader, '_api', lambda: api)
    assert _audit_bytes(str(tmp_path), 'dir/file', 100) == b'original'
    assert observed == [True]
    directory.rename(tmp_path / 'moved')


def test_failed_open_closes_ancestor_handles(tmp_path):
    directory = tmp_path / 'dir'
    directory.mkdir()
    with pytest.raises(OSError):
        _audit_bytes(str(tmp_path), 'dir/missing', 10)
    directory.rename(tmp_path / 'moved')


def test_nested_terminal_uses_test_interpreter(tmp_path):
    from tools.terminal_tool import terminal_tool, cleanup_all_environments
    cleanup_all_environments()
    try:
        result = terminal_tool('python -c "import sys, pytest; print(sys.executable)"',
                               workdir=str(tmp_path))
        import json
        result = json.loads(result) if isinstance(result, str) else result
        assert result.get('exit_code') == 0, result
        assert os.path.normcase(sys.executable) in os.path.normcase(result.get('output', '')), result
    finally:
        cleanup_all_environments()

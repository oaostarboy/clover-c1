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


def test_junction_above_project_is_rejected(tmp_path):
    real = tmp_path / 'real'
    (real / 'project').mkdir(parents=True)
    (real / 'project' / 'file').write_bytes(b'original')
    assert _audit_bytes(str(real / 'project'), 'file', 100) == b'original'
    link = tmp_path / 'link'
    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(real)],
                   check=True, capture_output=True)
    try:
        with pytest.raises((OSError, ValueError)):
            _audit_bytes(str(link / 'project'), 'file', 100)
    finally:
        os.rmdir(link)


def test_subst_drive_is_not_an_unchecked_project_root(tmp_path):
    import ctypes
    (tmp_path / 'file').write_bytes(b'original')
    assert _audit_bytes(str(tmp_path), 'file', 100) == b'original'
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    drive = next(chr(n + 65) + ':' for n in range(25, 3, -1) if not mask & (1 << n))
    subprocess.run(['subst.exe', drive, str(tmp_path)], check=True, capture_output=True)
    try:
        with pytest.raises(ValueError, match='direct local volume'):
            _audit_bytes(drive + '\\', 'file', 100)
    finally:
        subprocess.run(['subst.exe', drive, '/d'], check=True, capture_output=True)


def test_component_replacement_before_child_open_is_excluded(tmp_path, monkeypatch):
    directory = tmp_path / 'dir'
    directory.mkdir()
    (directory / 'file').write_bytes(b'original')
    api = reader._api()
    original = api.open
    observed = []

    def opening(parent, part, directory_flag):
        if part == 'file':
            with pytest.raises(OSError):
                directory.rename(tmp_path / 'moved')
            observed.append(True)
        return original(parent, part, directory_flag)

    api.open = opening
    monkeypatch.setattr(reader, '_api', lambda: api)
    assert _audit_bytes(str(tmp_path), 'dir/file', 100) == b'original'
    assert observed == [True]


def test_writable_mapped_view_cannot_mutate_a_read(tmp_path, monkeypatch):
    import ctypes as c
    from ctypes import wintypes as w
    path = tmp_path / 'file'
    path.write_bytes(b'original')
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateFileMappingW.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, w.DWORD, w.DWORD, w.LPCWSTR]
    kernel.CreateFileMappingW.restype = w.HANDLE
    kernel.MapViewOfFile.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, c.c_size_t]
    kernel.MapViewOfFile.restype = w.LPVOID
    kernel.UnmapViewOfFile.argtypes = [w.LPVOID]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateFileW(str(path), 0xC0000000, 7, None, 3, 0x80, None)
    assert handle != w.HANDLE(-1).value
    mapping = kernel.CreateFileMappingW(handle, None, 4, 0, 0, None)
    assert mapping
    view = kernel.MapViewOfFile(mapping, 2, 0, 0, 0)
    assert view
    kernel.CloseHandle(handle)
    api = reader._api()
    original = api.read
    mutated = []

    def during(file, size):
        c.memmove(view, b'mutated!', 8)
        mutated.append(True)
        return original(file, size)

    api.read = during
    monkeypatch.setattr(reader, '_api', lambda: api)
    try:
        with pytest.raises((OSError, ValueError)):
            _audit_bytes(str(tmp_path), 'file', 100)
    finally:
        kernel.UnmapViewOfFile(view)
        kernel.CloseHandle(mapping)
    # Either the reader refused the existing mapping or detected the real write.
    assert path.read_bytes() in {b'original', b'mutated!'}


@pytest.mark.parametrize('failure', ['size', 'reparse', 'read'])
def test_rejection_closes_all_handles(tmp_path, monkeypatch, failure):
    directory = tmp_path / 'dir'
    directory.mkdir()
    path = directory / 'file'
    path.write_bytes(b'original')
    limit = 2 if failure == 'size' else 100
    api = reader._api()
    if failure == 'read':
        def fail_read(*args):
            raise OSError('injected I/O error after actual handle acquisition')
        api.read = fail_read
    if failure == 'reparse':
        path.unlink()
        os.symlink(tmp_path / 'outside', path)
    monkeypatch.setattr(reader, '_api', lambda: api)
    with pytest.raises((OSError, ValueError)):
        _audit_bytes(str(tmp_path), 'dir/file', limit)
    directory.rename(tmp_path / 'moved')

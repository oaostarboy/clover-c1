"""Windows path grammar is checked before any native handle is opened."""
import pytest

from agent import work_lifecycle


@pytest.mark.parametrize('name', ['x:y', 'a\\b', 'CON', 'aux.txt', 'dir./x', 'dir /x', '../x', '/x', 'a//b', 'x\x00y'])
def test_windows_namespace_paths_are_rejected_before_io(name):
    from agent.windows_audit_reader import validated_parts
    with pytest.raises(ValueError):
        validated_parts('C:\\project', name, 1024)


def test_windows_path_parts_are_literal():
    from agent.windows_audit_reader import validated_parts
    assert validated_parts('C:\\Users\\runner\\project', 'dir/file.json', 8192) == (
        'C:\\', ('Users', 'runner', 'project', 'dir', 'file.json'))


def test_windows_reader_dispatches_without_posix_open(monkeypatch):
    import sys
    from agent import windows_audit_reader
    monkeypatch.setattr(sys, 'platform', 'win32')
    observed = []
    monkeypatch.setattr(windows_audit_reader, 'read_bytes', lambda *args: observed.append(args) or b'checked')
    assert work_lifecycle._audit_bytes('C:\\project', 'file.json', 123) == b'checked'
    assert observed == [('C:\\project', 'file.json', 123)]

"""Native SID/DACL and retained NT handle proof; no emulated security passes."""
import os
from pathlib import Path
import subprocess

import pytest

from clover_cli.readonly_files import ReadOnlyStateError, read_existing_file

pytestmark = pytest.mark.windows_only


def secure_acl(path, *, public=False, extra_mask=None, owner=None, null=False):
    """Provision ONLY disposable fixtures, never repair inside the reader."""
    import ntsecuritycon as rights
    import win32security as security
    from clover_cli.windows_ssh_runtime import _current_sid
    user = _current_sid()
    acl = None if null else security.ACL()
    if acl is not None:
        for sid in (user, security.ConvertStringSidToSid('S-1-5-18'),
                    security.ConvertStringSidToSid('S-1-5-32-544')):
            acl.AddAccessAllowedAceEx(security.ACL_REVISION, 3, rights.FILE_ALL_ACCESS, sid)
        mask = extra_mask if extra_mask is not None else (rights.FILE_GENERIC_READ if public else 0)
        if mask:
            acl.AddAccessAllowedAceEx(security.ACL_REVISION, 0, mask,
                                     security.ConvertStringSidToSid('S-1-1-0'))
    flags = security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION
    if owner is not None:
        flags |= security.OWNER_SECURITY_INFORMATION
    security.SetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT, flags,
                                 owner, None, acl, None)


@pytest.fixture
def state(tmp_path):
    home = tmp_path / 'private'
    home.mkdir()
    secure_acl(home)
    path = home / 'config.yaml'
    path.write_bytes(b'\xef\xbb\xbfmodel: exact\n')
    return home, path


def test_private_positive_is_bounded_fresh_and_read_only(state):
    home, path = state
    import win32security as security
    flags = security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION
    before = (path.read_bytes(), bytes(security.GetFileSecurity(str(path), flags)),
              sorted(p.name for p in home.iterdir()), dict(os.environ))
    assert read_existing_file(path) == 'model: exact\n'
    path.write_bytes(b'fresh: true\n')
    assert read_existing_file(path) == 'fresh: true\n'
    path.write_bytes(before[0])
    assert (path.read_bytes(), bytes(security.GetFileSecurity(str(path), flags)),
            sorted(p.name for p in home.iterdir()), dict(os.environ)) == before


@pytest.mark.parametrize('target', ['leaf', 'parent'])
@pytest.mark.parametrize('mask', [1, 2, 0x40000, 0x80000, 0x10000])
def test_foreign_private_permissions_rejected_before_read(state, monkeypatch, target, mask):
    home, path = state
    secure_acl(path if target == 'leaf' else home, extra_mask=mask)
    from agent import windows_audit_reader
    api = windows_audit_reader._api()
    def forbidden(*args):
        pytest.fail('data read before rejecting permissive native ACL')
    api.read = forbidden
    monkeypatch.setattr(windows_audit_reader, '_api', lambda: api)
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(path)
    # Rejection closes every handle.
    path.rename(home / 'renamed')


def test_public_policy_allows_foreign_read_not_write(state):
    home, path = state
    secure_acl(path, public=True)
    assert read_existing_file(path, private=False) == 'model: exact\n'
    secure_acl(path, extra_mask=2)
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(path, private=False)


def test_null_dacl_rejected(state):
    _, path = state
    secure_acl(path, null=True)
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(path)


def test_wrong_private_owner_rejected(state):
    import win32security as security
    _, path = state
    # Administrator group is assignable to the hosted runner's elevated token.
    secure_acl(path, owner=security.ConvertStringSidToSid('S-1-5-32-544'))
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(path)


@pytest.mark.parametrize('where', ['ancestor', 'parent', 'leaf'])
def test_native_junction_refused(state, tmp_path, where):
    home, path = state
    link = tmp_path / 'junction'
    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(home)],
                   check=True, capture_output=True)
    try:
        selected = {'ancestor': link / 'nested' / 'config.yaml',
                    'parent': link / path.name, 'leaf': link}[where]
        with pytest.raises(ReadOnlyStateError):
            read_existing_file(selected)
    finally:
        os.rmdir(link)


def test_native_leaf_symlink_refused(state):
    home, path = state
    link = home / 'alias'
    os.symlink(path, link)
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(link)


@pytest.mark.parametrize('kind', ['oversize', 'directory', 'encoding'])
def test_native_invalid_data_refused_and_handles_closed(state, kind):
    home, path = state
    if kind == 'directory':
        path.unlink()
        path.mkdir()
    elif kind == 'encoding':
        path.write_bytes(b'\xff')
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(path, limit=2 if kind == 'oversize' else 100)
    path.rename(home / 'released')


def test_existing_writer_refused(state):
    _, path = state
    with path.open('r+b'):
        with pytest.raises(ReadOnlyStateError):
            read_existing_file(path)


def test_read_pins_parent_and_leaf(state, monkeypatch):
    home, path = state
    from agent import windows_audit_reader
    api = windows_audit_reader._api()
    original = api.read
    reached = []
    def during(handle, size):
        for operation in (lambda: path.write_bytes(b'mutation'), lambda: path.unlink(),
                          lambda: home.rename(home.with_name('moved'))):
            with pytest.raises(OSError):
                operation()
        reached.append(True)
        return original(handle, size)
    api.read = during
    monkeypatch.setattr(windows_audit_reader, '_api', lambda: api)
    assert read_existing_file(path) == 'model: exact\n'
    assert reached == [True]
    home.rename(home.with_name('moved'))


def test_read_only_config_real_scope(state, monkeypatch):
    home, path = state
    from clover_cli import config
    from clover_cli.readonly_files import non_mutating_config_reads
    path.write_text('model:\n  provider: openai-codex\n  default: exact-model\n', encoding='utf-8')
    monkeypatch.setenv('CLOVER_HOME', str(home))
    def forbidden(*args, **kwargs):
        pytest.fail('read-only Windows config initialized or migrated')
    monkeypatch.setattr(config, 'ensure_clover_home', forbidden)
    monkeypatch.setattr(config, 'migrate_config', forbidden)
    with non_mutating_config_reads():
        assert config.load_config()['model']['default'] == 'exact-model'
        assert config.load_config_readonly()['model']['default'] == 'exact-model'


def test_live_quota_unsupported_before_mutation(state):
    from scripts.eval_presence_current_turn import _reserve_isolated_call
    home, _ = state
    before = sorted(p.name for p in home.iterdir())
    with pytest.raises(ReadOnlyStateError, match='evaluation_quota_unsupported'):
        _reserve_isolated_call(home, 'synthetic', 1)
    assert sorted(p.name for p in home.iterdir()) == before

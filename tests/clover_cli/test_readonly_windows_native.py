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


def test_owner_rights_is_bound_to_actual_validated_owner(state):
    import win32security as security
    home, path = state
    # Construct the native OWNER RIGHTS ACL explicitly so this test does not
    # depend on which CPython versions also emit it from mkdir(mode=0700).
    native = home / 'native-private'
    native.mkdir()
    acl = security.ACL()
    for sid in ('S-1-3-4', 'S-1-5-18', 'S-1-5-32-544'):
        acl.AddAccessAllowedAceEx(security.ACL_REVISION, 3, 0x1F01FF,
                                 security.ConvertStringSidToSid(sid))
    security.SetNamedSecurityInfo(str(native), security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None)
    selected = native / path.name
    selected.write_bytes(path.read_bytes())
    assert read_existing_file(selected) == 'model: exact\n'
    secure_acl(selected, owner=security.ConvertStringSidToSid('S-1-5-32-544'))
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(selected)


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
    if where == 'ancestor':
        (home / 'nested').mkdir()
        (home / 'nested' / path.name).write_bytes(path.read_bytes())
        assert read_existing_file(home / 'nested' / path.name) == 'model: exact\n'
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


def test_native_cold_selection_under_real_evaluation_guard(tmp_path, monkeypatch, record_property):
    import json
    import sys
    from tests.clover_cli.test_runtime_provider_readonly import profile, inventory
    home, _, _ = profile(monkeypatch, tmp_path)
    secure_acl(home)
    before = inventory(home)
    code = r'''
import json, os, sys
from pathlib import Path
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard
home = Path(os.environ['CLOVER_HOME'])
guard = ReadOnlyEvaluationGuard(home=home, source=Path.cwd(), write_dirs=[])
guard.install()
from clover_cli.runtime_provider import resolve_runtime_provider
runtime = resolve_runtime_provider(read_only=True)
assert runtime['credential_pool'].current() is not None
blocked = 0
import socket
for action in (lambda: (home/'not-allowed').write_bytes(b'no'),
               lambda: socket.getaddrinfo('example.invalid', 443)):
    try:
        action()
    except PermissionError:
        blocked += 1
file_baseline_unchanged = guard.baseline == guard._baseline()
environment_changes = sorted(k for k in set(guard.environment) | set(os.environ)
                             if guard.environment.get(k) != os.environ.get(k))
print(json.dumps({'selected':True, 'blocked':blocked, 'guard':guard.receipt(),
                  'file_baseline_unchanged':file_baseline_unchanged,
                  'environment_change_keys':environment_changes}))
'''
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT') if k in os.environ}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), CLOVER_HOME=str(home),
               CLOVER_MANAGED_DIR=str(tmp_path/'managed'), PYTHONPATH=str(root),
               # Preserve the canonical runner's UTF-8 child contract. Without
               # it, the ordinary CLI stdout repair (not a config read) seeds
               # its documented UTF-8 env defaults after the audit baseline.
               PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    child = subprocess.run([sys.executable, '-c', code], cwd=root, env=env,
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result['selected'] and result['blocked'] == 2
    record_property('cold_file_baseline_unchanged', result['file_baseline_unchanged'])
    record_property('cold_environment_change_keys', json.dumps(result['environment_change_keys']))
    assert result['guard']['baseline_unchanged'], json.dumps({
        'files_unchanged':result['file_baseline_unchanged'], 'changed_keys':result['environment_change_keys']})
    assert len(result['guard']['denied_writes']) == 1
    assert result['guard']['network_events'][0]['admitted'] is False
    assert result['guard']['denied_private_reads'] == []
    assert inventory(home) == before


def test_foreign_ancestor_mutation_refused(state, tmp_path, monkeypatch):
    _, path = state
    assert read_existing_file(path) == 'model: exact\n'
    secure_acl(tmp_path, extra_mask=0x40)  # FILE_DELETE_CHILD on an ancestor.
    from agent import windows_audit_reader
    api = windows_audit_reader._api()
    def forbidden(*args):
        pytest.fail('data read below foreign-mutable ancestor')
    api.read = forbidden
    monkeypatch.setattr(windows_audit_reader, '_api', lambda: api)
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(path)


def test_retained_writable_mapping_refused(state, monkeypatch, record_property):
    home, path = state
    assert read_existing_file(path) == 'model: exact\n'
    from tests.agent import test_windows_audit_native as existing
    def selected(project, name, limit):
        return read_existing_file(Path(project) / name, limit=limit)
    monkeypatch.setattr(existing, '_audit_bytes', selected)
    # Reuse the real Win32 mapping/mutation seam, including its reach/refusal
    # receipts. The test creates only its own disposable file below secure home.
    existing.test_writable_mapped_view_cannot_mutate_a_read(home, monkeypatch, record_property)


def test_native_acl_change_during_read_refused(state, monkeypatch):
    _, path = state
    from agent import windows_audit_reader
    api = windows_audit_reader._api()
    original = api.read
    reached = []
    def during(handle, size):
        secure_acl(path, extra_mask=1)
        reached.append(True)
        return original(handle, size)
    api.read = during
    monkeypatch.setattr(windows_audit_reader, '_api', lambda: api)
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(path)
    assert reached == [True]


@pytest.mark.parametrize('tail', ['file:stream', 'NUL', 'file.', 'file ', '../file', './file'])
def test_literal_aliases_refused(state, tail):
    home, _ = state
    with pytest.raises(ReadOnlyStateError):
        read_existing_file(str(home) + '/' + tail)

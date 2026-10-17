"""Shared pairing writer regressions; injected failures are not native ACL proof."""

import errno
import os
from pathlib import Path
import tempfile
from types import FunctionType, SimpleNamespace

import pytest

from gateway import pairing


@pytest.fixture
def writer_dir(tmp_path):
    directory = tmp_path / "writer"
    directory.mkdir()
    return directory


def _deny_temp_creation(monkeypatch):
    """Deny the real exclusive-create seam; bound an erroneous retry to two calls."""
    original = os.open
    denied = PermissionError(errno.EACCES, "injected directory create denial")
    attempts = []

    def open_file(path, flags, mode=0o777, **kwargs):
        if flags & os.O_EXCL and str(path).endswith(".tmp"):
            attempts.append(str(path))
            if len(attempts) > 1:
                raise AssertionError("exclusive creation retried after access denial")
            raise denied
        return original(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    return denied, attempts


def _retrying_tempfile_dependency(monkeypatch):
    """Exercise CPython's actual retry algorithm with a dependency-only adapter.

    No interpreter/platform globals are changed. This isolates the observed
    tempfile branch: directory exists and os.access says writable despite an
    os.open EACCES. It is fault injection, not Windows/NTFS certification.
    """
    dependency_os = SimpleNamespace(
        name="nt", path=os.path, access=lambda *args: True,
        W_OK=os.W_OK, open=os.open,
    )
    algorithm = getattr(tempfile, "_mkstemp_inner")
    globals_ = dict(algorithm.__globals__, _os=dependency_os)
    inner = FunctionType(algorithm.__code__, globals_)

    def mkstemp(*, dir, suffix):
        return inner(dir, "tmp", suffix, getattr(tempfile, "_bin_openflags"), str)

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)


def test_owner_confirmation_denial_is_not_retried_or_granted(writer_dir, monkeypatch):
    store = pairing.VerifiedOwnerStore("123:test-token", home=writer_dir)
    code = store.prove(store.begin(), "12345678")
    assert code
    before = store.path.read_bytes()
    files = set(store.path.parent.iterdir())
    denied, attempts = _deny_temp_creation(monkeypatch)
    _retrying_tempfile_dependency(monkeypatch)

    try:
        with pytest.raises(PermissionError) as caught:
            store.confirm(code)
        assert caught.value is denied
        assert len(attempts) == 1
    finally:
        assert store.path.read_bytes() == before
        assert not store.is_owner("12345678")
        assert set(store.path.parent.iterdir()) == files


@pytest.mark.parametrize("operation", ["approve", "revoke"])
def test_pairing_approval_denial_preserves_records_and_grants_nothing(writer_dir, monkeypatch, operation):
    monkeypatch.setattr(pairing, "PAIRING_DIR", writer_dir / "pairing")
    store = pairing.PairingStore()
    old_code = store.generate_code("telegram", "100")
    assert old_code
    approved = store.approve_code("telegram", old_code)
    assert approved and approved["user_id"] == "100"
    new_code = store.generate_code("telegram", "200")
    assert new_code
    before = {p: p.read_bytes() for p in store._dir.iterdir()}
    denied, attempts = _deny_temp_creation(monkeypatch)
    _retrying_tempfile_dependency(monkeypatch)

    try:
        with pytest.raises(PermissionError) as caught:
            if operation == "approve":
                store.approve_code("telegram", new_code)
            else:
                store.revoke("telegram", "100")
        assert caught.value is denied
        assert len(attempts) == 1
    finally:
        assert {p: p.read_bytes() for p in store._dir.iterdir()} == before
        assert store.is_approved("telegram", "100")
        assert not store.is_approved("telegram", "200")


def test_real_file_collision_is_retried_without_touching_the_collision(writer_dir, monkeypatch):
    collision = writer_dir / ("tmp" + "a" * 32 + ".tmp")
    collision.write_bytes(b"not ours")
    tokens = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(pairing.secrets, "token_hex", lambda size: next(tokens))
    original = os.open
    attempts = []

    def open_file(path, flags, mode=0o777, **kwargs):
        if str(path).endswith(".tmp"):
            attempts.append(Path(path))
            assert flags & os.O_EXCL
            assert flags & os.O_CREAT
            assert flags & os.O_RDWR
            assert mode == 0o600
        return original(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    target = writer_dir / "record.json"
    pairing._secure_write(target, "new\nrecord: \u2713")
    assert attempts == [collision, writer_dir / ("tmp" + "b" * 32 + ".tmp")]
    assert target.read_text(encoding="utf-8") == "new\nrecord: \u2713"
    assert collision.read_bytes() == b"not ours"
    assert set(writer_dir.iterdir()) == {target, collision}


def test_collision_exhaustion_is_bounded_and_preserves_previous_record(writer_dir, monkeypatch):
    collision = writer_dir / ("tmp" + "a" * 32 + ".tmp")
    collision.write_bytes(b"not ours")
    target = writer_dir / "record.json"
    target.write_bytes(b"previous")
    monkeypatch.setattr(pairing.secrets, "token_hex", lambda size: "a" * 32)
    original = os.open
    attempts = []

    def open_file(path, flags, mode=0o777, **kwargs):
        if flags & os.O_EXCL and str(path).endswith(".tmp"):
            attempts.append(path)
            assert len(attempts) <= 100, "collision retries must be bounded"
        return original(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(FileExistsError) as caught:
        pairing._secure_write(target, "new")
    assert caught.value.errno == errno.EEXIST
    assert len(attempts) == 100
    assert target.read_bytes() == b"previous"
    assert collision.read_bytes() == b"not ours"
    assert set(writer_dir.iterdir()) == {target, collision}


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM, errno.ENOENT, errno.ENOSPC, errno.EIO])
def test_noncollision_creation_errors_propagate_once(writer_dir, monkeypatch, error_number):
    target = writer_dir / "record.json"
    target.write_bytes(b"previous")
    original = os.open
    failure = OSError(error_number, "injected create failure")
    attempts = []

    def open_file(path, flags, mode=0o777, **kwargs):
        if flags & os.O_EXCL and str(path).endswith(".tmp"):
            attempts.append(path)
            raise failure
        return original(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(OSError) as caught:
        pairing._secure_write(target, "new")
    assert caught.value is failure
    assert len(attempts) == 1
    assert target.read_bytes() == b"previous"
    assert list(writer_dir.iterdir()) == [target]


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt])
def test_postcreation_failure_cleans_only_our_temp(writer_dir, monkeypatch, failure_point, failure_type):
    target = writer_dir / "record.json"
    target.write_bytes(b"previous")
    unrelated = writer_dir / "other.tmp"
    unrelated.write_bytes(b"not ours")
    failure = failure_type("injected postcreation failure")
    seen = []
    original = os.open

    def open_file(path, flags, mode=0o777, **kwargs):
        fd = original(path, flags, mode, **kwargs)
        if flags & os.O_EXCL and str(path).endswith(".tmp"):
            seen.append((Path(path), fd))
        return fd

    def fail(*args):
        assert len(seen) == 1
        if failure_point == "replace":
            assert seen[0][0].read_text(encoding="utf-8") == "new"
        raise failure

    monkeypatch.setattr(os, "open", open_file)
    if failure_point == "fsync":
        monkeypatch.setattr(os, "fsync", fail)
    else:
        monkeypatch.setattr(pairing, "atomic_replace", fail)
    with pytest.raises(failure_type) as caught:
        pairing._secure_write(target, "new")
    assert caught.value is failure
    assert target.read_bytes() == b"previous"
    assert unrelated.read_bytes() == b"not ours"
    assert set(writer_dir.iterdir()) == {target, unrelated}
    with pytest.raises(OSError) as closed:
        os.fstat(seen[0][1])
    assert closed.value.errno == errno.EBADF


def test_temp_is_flushed_and_fsynced_before_real_replace(writer_dir, monkeypatch):
    target = writer_dir / "record.json"
    target.write_bytes(b"previous")
    events = []
    original_fsync = os.fsync
    original_replace = pairing.atomic_replace

    def fsync(fd):
        events.append("fsync")
        assert target.read_bytes() == b"previous"
        temps = list(writer_dir.glob("*.tmp"))
        assert len(temps) == 1
        assert temps[0].read_bytes() == b"new"
        assert not os.get_inheritable(fd)
        return original_fsync(fd)

    def replace(temp, destination):
        assert events == ["fsync"]
        assert Path(temp).parent == target.parent
        events.append("replace")
        return original_replace(temp, destination)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(pairing, "atomic_replace", replace)
    pairing._secure_write(target, "new")
    assert events == ["fsync", "replace"]
    assert target.read_bytes() == b"new"
    assert list(writer_dir.iterdir()) == [target]


@pytest.mark.linux_only
def test_temp_creation_and_target_stay_private(writer_dir, monkeypatch):
    import stat

    target = writer_dir / "record.json"
    original = os.fsync

    def fsync(fd):
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    pairing._secure_write(target, "private")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.linux_only
@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_existing_candidate_is_never_overwritten(writer_dir, monkeypatch, kind):
    collision = writer_dir / ("tmp" + "a" * 32 + ".tmp")
    victim = writer_dir / "victim"
    victim.write_bytes(b"not ours")
    if kind == "file":
        collision.write_bytes(b"existing file")
    elif kind == "symlink":
        collision.symlink_to(victim)
    else:
        collision.mkdir()
    tokens = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(pairing.secrets, "token_hex", lambda size: next(tokens))
    target = writer_dir / "record.json"
    pairing._secure_write(target, "new")
    assert target.read_bytes() == b"new"
    assert victim.read_bytes() == b"not ours"
    assert collision.exists()
    if kind == "file":
        assert collision.read_bytes() == b"existing file"
    elif kind == "symlink":
        assert collision.is_symlink()
    else:
        assert collision.is_dir()
        assert not list(collision.iterdir())
    assert set(writer_dir.iterdir()) == {collision, victim, target}


@pytest.mark.linux_only
def test_secure_write_retains_inherited_target_symlink_following(writer_dir):
    target = writer_dir / "record.json"
    real = writer_dir / "real.json"
    real.write_bytes(b"previous")
    target.symlink_to(real)
    pairing._secure_write(target, "new")
    assert target.is_symlink()
    assert real.read_bytes() == b"new"
    assert set(writer_dir.iterdir()) == {target, real}


def test_owner_readback_failure_does_not_report_success(writer_dir, monkeypatch):
    store = pairing.VerifiedOwnerStore("123:test-token", home=writer_dir)
    code = store.prove(store.begin(), "12345678")
    assert code
    original = Path.read_text
    failure = PermissionError(errno.EACCES, "injected postwrite read denial")
    reads = []

    def read_text(path, *args, **kwargs):
        if path == store.path:
            reads.append(path)
            if len(reads) > 1:
                raise failure
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(PermissionError) as caught:
        store.confirm(code)
    assert caught.value is failure
    assert len(reads) == 2
    assert not store.is_owner("12345678")
    assert not list(store.path.parent.glob("*.tmp"))

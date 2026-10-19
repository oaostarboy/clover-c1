"""Fail-closed reads of existing settings/credentials; never repair the filesystem.

Descriptor-relative traversal rejects symlinks (including ancestor swaps). This
contract currently requires POSIX; callers on other systems fail before reading
secrets, rather than pretending mode bits validate a Windows ACL.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
from contextlib import contextmanager
from contextvars import ContextVar

_CONFIG_READ_ONLY = ContextVar('clover_non_mutating_config_reads', default=False)


def non_mutating_config_active():
    return _CONFIG_READ_ONLY.get()


@contextmanager
def non_mutating_config_reads():
    """Task-local contract for nested config/TLS readers, not a cache shim.

    All public config loaders use fresh validated file reads in this scope.
    Normal callers retain initialization/migration. Context resets on every exit.
    """
    token = _CONFIG_READ_ONLY.set(True)
    try:
        yield
    finally:
        _CONFIG_READ_ONLY.reset(token)


class ReadOnlyStateError(ValueError):
    """Existing state cannot safely authorize an evaluation (no secret details)."""


def read_existing_file(path: Path, *, private: bool = True, limit: int = 2097152) -> str:
    """Read a bounded regular file through secure directory handles, without caches.

    Private files and their direct parent must belong to the effective user and
    have no group/other permissions. Public administrator policy may be readable
    but not writable by group/other, and must belong to root or the current user.
    Ancestors may be root/user-owned, non-writable or sticky shared directories.
    No chmod, mkdir, dotenv loading, lock-file creation, migration or fallback.
    """
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
        raise ReadOnlyStateError('secure_read_unsupported')
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ReadOnlyStateError('invalid_state_path')
    fd = None
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        for index, part in enumerate(path.parts[1:-1]):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            direct = index == len(path.parts) - 3
            owned = info.st_uid in (0, os.geteuid())
            unsafe = bool(info.st_mode & 0o022) and not bool(info.st_mode & stat.S_ISVTX)
            if not owned or unsafe or (private and direct and (
                    info.st_uid != os.geteuid() or info.st_mode & 0o077)):
                raise ReadOnlyStateError('insecure_state_directory')
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(fd)
        fd = file_fd
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                or before.st_uid not in ((os.geteuid(),) if private else (0, os.geteuid()))
                or before.st_mode & (0o077 if private else 0o022)):
            raise ReadOnlyStateError('insecure_state_file')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            content = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(content) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ReadOnlyStateError('state_changed_during_read')
        return content.decode('utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise ReadOnlyStateError('existing_state_unavailable') from exc
    finally:
        if fd is not None:
            os.close(fd)

"""Fail-closed reads of existing settings/credentials; never repair the filesystem.

Descriptor-relative traversal rejects symlinks (including ancestor swaps).
Windows uses the reviewed retained NT-handle reader with native SID/DACL checks,
not POSIX mode bits. Unsupported native/filesystem states fail before data reads.
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
    if os.name == 'nt':
        return _read_windows(path, private=private, limit=limit)
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'geteuid'):
        raise ReadOnlyStateError('secure_read_unsupported')
    uid = os.geteuid() if hasattr(os, 'geteuid') else None
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
            owned = info.st_uid in (0, uid)
            unsafe = bool(info.st_mode & 0o022) and not bool(info.st_mode & stat.S_ISVTX)
            if not owned or unsafe or (private and direct and (
                    info.st_uid != uid or info.st_mode & 0o077)):
                raise ReadOnlyStateError('insecure_state_directory')
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(fd)
        fd = file_fd
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                or before.st_uid not in ((uid,) if private else (0, uid))
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


def _read_windows(path, *, private, limit):
    """Reuse pinned NT traversal; validate the actual handle owner and DACL.

    SYSTEM/Administrators have root-equivalent authority on local Windows.
    TrustedInstaller is accepted as an ancestor/public-policy owner, not as a
    private-file owner or an extra private DACL reader. Unknown ACE semantics,
    remote volumes, reparse points and unavailable security APIs fail closed.
    """
    try:
        import sys
        raw = os.fspath(path)
        if not isinstance(raw, str):
            raise ReadOnlyStateError('invalid_state_path')
        # Raw NT I/O does not emit CPython's open event itself. Preserve the
        # enclosing audit guard's private-read scope BEFORE any handle/data I/O.
        sys.audit('open', raw, 'rb', os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        from agent.windows_audit_reader import read_bytes
        from clover_cli.windows_ssh_runtime import _current_sid
        import win32security as security

        user = security.ConvertSidToStringSid(_current_sid())
        privileged = {user, 'S-1-5-18', 'S-1-5-32-544'}
        owners = privileged | {
            'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'}
        fields = security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION
        # Generic/all/maximum, security, deletion and existing-data mutation.
        writes = 0x50000000 | 0x02000000 | 0x01000000 | 0x000D0000 | 0x156

        def verify(handle, directory, direct):
            descriptor = security.GetSecurityInfo(
                getattr(handle, 'value', handle), security.SE_FILE_OBJECT, fields)
            owner = security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
            restricted = private and (not directory or direct)
            if owner not in ({user} if restricted else owners):
                raise ReadOnlyStateError('insecure_state_owner')
            acl = descriptor.GetSecurityDescriptorDacl()
            if acl is None:
                raise ReadOnlyStateError('insecure_state_acl')
            for index in range(acl.GetAceCount()):
                ace = acl.GetAce(index)
                kind, flags = ace[0]
                if flags & 0x08:  # INHERIT_ONLY: no rights on this object.
                    continue
                if kind == security.ACCESS_DENIED_ACE_TYPE:
                    continue  # Ignoring denials only makes this check stricter.
                if kind != security.ACCESS_ALLOWED_ACE_TYPE:
                    raise ReadOnlyStateError('unsupported_state_acl')
                mask, sid = ace[1], security.ConvertSidToStringSid(ace[-1])
                # OWNER RIGHTS is an object-relative principal, not Everyone or
                # CREATOR OWNER. Bind it only to this handle's validated owner.
                # CPython's native mkdir(0700) uses this ACL on Windows.
                if sid == 'S-1-3-4':
                    sid = owner
                if sid in (privileged if restricted else owners):
                    continue
                # Ancestors may allow creation (ADD_FILE/ADD_SUBDIRECTORY), as
                # sticky shared POSIX directories do. They must not allow
                # deletion of children, ACL/owner changes or in-place mutation.
                forbidden = writes & ~0x06 if directory and not direct else writes
                if mask and (restricted or mask & forbidden):
                    raise ReadOnlyStateError('insecure_state_acl')
            return security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                descriptor, security.SDDL_REVISION_1, fields)

        # Keep literal path components until the reviewed validator sees them;
        # Path(...) would normalize dot/trailing separators before validation.
        split = max(raw.rfind('/'), raw.rfind('\\'))
        if split < 2:
            raise ReadOnlyStateError('invalid_state_path')
        project = raw[:split] if split > 2 else raw[:split + 1]
        value = read_bytes(project, raw[split + 1:], limit, verify_handle=verify)
        return value.decode('utf-8-sig')
    except ReadOnlyStateError:
        raise
    except Exception as exc:
        raise ReadOnlyStateError('existing_state_unavailable') from exc

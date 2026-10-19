"""OS audit boundary shared by offline proof and the real isolated smoke runner.

No permission grant or monkeypatch: forbidden syscalls raise before execution.
Network starts denied and may be admitted only around a durably charged provider
entry by the caller. Do not disable this guard for the real smoke.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import stat
from contextvars import ContextVar


# Python's open audit event omits dir_fd. Preserve that addressing information
# during the syscall, NOT a permission bit: the audit hook still validates the
# actual directory and leaf. Never exempt this open from the installed hook.
_OPEN_DIR_FD = ContextVar('presence_evaluation_open_dir_fd', default=None)


def evaluation_open(path, flags, mode=0o600, *, dir_fd=None):
    token = _OPEN_DIR_FD.set(dir_fd)
    try:
        return os.open(path, flags, mode, dir_fd=dir_fd)
    finally:
        _OPEN_DIR_FD.reset(token)


def _windows_readonly_pipe(value):
    """Prove a non-filesystem pipe and kernel-granted read-only access.

    fcntl is unavailable on Windows. Do not substitute a descriptor/mode label:
    writable, unknown, regular-file and character handles remain ungranted.
    """
    if os.name != 'nt' or type(value) is not int:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        if not stat.S_ISFIFO(os.fstat(value).st_mode):
            return False
        handle = msvcrt.get_osfhandle(value)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kind = kernel.GetFileType
        kind.argtypes = [wintypes.HANDLE]
        kind.restype = wintypes.DWORD
        if kind(handle) != 3:  # FILE_TYPE_PIPE, not a disk or character device.
            return False

        class ObjectBasicInformation(ctypes.Structure):
            _fields_ = [('Attributes', wintypes.ULONG), ('GrantedAccess', wintypes.ULONG),
                       ('HandleCount', wintypes.ULONG), ('PointerCount', wintypes.ULONG),
                       ('Reserved', wintypes.ULONG * 10)]

        query = ctypes.WinDLL('ntdll').NtQueryObject
        query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                          wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        query.restype = wintypes.LONG
        info, returned = ObjectBasicInformation(), wintypes.ULONG()
        status = query(handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned))
        # Only concrete FILE_READ_DATA/READ_EA/READ_ATTRIBUTES/READ_CONTROL/
        # SYNCHRONIZE rights. Unknown or any write/delete/ownership rights deny.
        allowed = 0x00120089
        return (status == 0 and returned.value == ctypes.sizeof(info)
                and bool(info.GrantedAccess & 0x1) and not info.GrantedAccess & ~allowed
                and msvcrt.get_osfhandle(value) == handle
                and stat.S_ISFIFO(os.fstat(value).st_mode) and kind(handle) == 3)
    except Exception:
        return False


class ReadOnlyEvaluationGuard:
    def __init__(self, *, home, source, write_dirs):
        self.home = Path(home).absolute()
        self.source = Path(source).resolve()
        self.write_dirs = tuple(Path(p).absolute() for p in write_dirs)
        self._write_roots = {}
        uid = os.geteuid() if hasattr(os, 'geteuid') else None
        for root in self.write_dirs:
            info = root.lstat()
            if (uid is None or root != root.resolve(strict=True) or '..' in root.parts
                    or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
                    or info.st_mode & 0o077):
                raise PermissionError('insecure_evaluation_directory')
            self._write_roots[root] = (info.st_dev, info.st_ino)
        self.environment = dict(os.environ)
        self.baseline = self._baseline()
        self.denied_writes = []
        self.denied_private_reads = []
        self.network_events = []
        self.auth_reads = 0
        self.provider_admitted = False

    def _baseline(self):
        out = {}
        for path in [self.home, *(self.home / n for n in ('config.yaml', 'auth.json', '.env'))]:
            try:
                info = path.lstat()
                out[path.name] = [info.st_mode, info.st_uid, info.st_gid,
                    hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None]
            except FileNotFoundError:
                out[path.name] = None
        return out

    def _path(self, value):
        if isinstance(value, int):
            # fdopen emits a second open audit event with the existing fd. Do
            # not blanket-allow descriptors: resolve and check the exact target.
            try:
                if sys.platform == 'darwin':
                    import fcntl
                    # Darwin has no /proc/self/fd. F_GETPATH resolves the open
                    # object in the kernel; failure is not a descriptor grant.
                    raw = fcntl.fcntl(value, getattr(fcntl, 'F_GETPATH', 50), bytes(1024))
                    target = os.fsdecode(raw.split(b'\0', 1)[0])
                else:
                    target = os.readlink('/proc/self/fd/' + str(value))
                return Path(target) if os.path.isabs(target) else None
            except (OSError, ValueError):
                return None
        if isinstance(value, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(value))
            if '..' in path.parts:
                return None
            directory_fd = _OPEN_DIR_FD.get()
            if not path.is_absolute() and directory_fd is not None:
                directory = self._path(directory_fd)
                return directory / path if directory is not None else None
            return path.absolute()
        return None

    def _within(self, path, roots):
        return path is not None and '..' not in path.parts and any(
            path == root or root in path.parents for root in roots)

    def _write_allowed(self, value, path, flags=0):
        """Authorize an anchored leaf/object, never lexical string containment.

        Plain string opens cannot pin a parent across the audit/syscall gap.
        Only NOFOLLOW directory-relative opens carry a usable anchor. Descriptor
        wrapping/truncation must refer to that same single-link private object.
        """
        uid = os.geteuid() if hasattr(os, 'geteuid') else None
        if uid is None or path is None or path.parent not in self._write_roots:
            return False
        try:
            root = path.parent
            root_info = root.lstat()
            if ((root_info.st_dev, root_info.st_ino) != self._write_roots[root]
                    or not stat.S_ISDIR(root_info.st_mode)
                    or root_info.st_uid != uid or root_info.st_mode & 0o077):
                return False
            if isinstance(value, int):
                info = os.fstat(value)
                named = path.lstat()
                if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                    return False
            else:
                directory_fd = _OPEN_DIR_FD.get()
                if (directory_fd is None or Path(os.fsdecode(value)).name != os.fsdecode(value)
                        or not flags & os.O_NOFOLLOW or flags & (os.O_TRUNC | os.O_APPEND)):
                    return False
                directory = os.fstat(directory_fd)
                if (directory.st_dev, directory.st_ino) != self._write_roots[root]:
                    return False
                try:
                    info = os.stat(value, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    # A non-creating open can only fail, never create a leaf.
                    return not flags & os.O_CREAT or bool(flags & os.O_EXCL)
            return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and info.st_uid == uid and not info.st_mode & 0o077)
        except (OSError, ValueError):
            return False

    def audit(self, event, args):
        if event == 'open':
            path = self._path(args[0])
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            # Permit only each OS's non-persistent null sink, not a pathname
            # ending in "nul" (extended namespaces can address real such files).
            null_sink = (os.name == 'posix' and path == Path('/dev/null')) or (
                os.name == 'nt' and type(args[0]) is str and args[0].casefold() == 'nul')
            if writing and not null_sink and not self._write_allowed(args[0], path, flags):
                self.denied_writes.append(event + ':' + str(path))
                raise PermissionError('evaluation_write_forbidden')
            if not writing:
                if path is None:
                    # Read-only pipe handles contain no private filesystem
                    # target. SDK platform-header discovery wraps one before
                    # its subprocess attempt (which remains forbidden below).
                    # Prove both object kind and actual OS access; never grant
                    # an unresolved regular file, writable pipe or unknown fd.
                    readonly_pipe = False
                    if isinstance(args[0], int):
                        try:
                            if os.name == 'nt':
                                readonly_pipe = _windows_readonly_pipe(args[0])
                            else:
                                import fcntl
                                import stat
                                readonly_pipe = (stat.S_ISFIFO(os.fstat(args[0]).st_mode)
                                    and fcntl.fcntl(args[0], fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY)
                        except (ImportError, OSError, ValueError):
                            pass
                    if not readonly_pipe:
                        self.denied_private_reads.append('unresolved_descriptor')
                        raise PermissionError('evaluation_private_read_forbidden')
                if path and path.name == 'auth.json':
                    self.auth_reads += 1
                allowed = (self.home / 'config.yaml', self.home / 'auth.json', self.home / '.env')
                roots = (self.source, Path(sys.prefix).resolve(), *self.write_dirs)
                if self._within(path, (self.home,)) and path not in allowed and not self._within(path, roots):
                    self.denied_private_reads.append(str(path))
                    raise PermissionError('evaluation_private_read_forbidden')
        if event in {'os.mkdir','os.chmod','os.chown','os.remove','os.rmdir','os.rename',
                     'os.link','os.symlink','os.truncate','os.utime','os.setxattr','os.removexattr'}:
            path = self._path(args[0]) if args else None
            # Quota charging only needs descriptor truncation. Namespace and
            # metadata mutations have no safe role here (rename/link have two
            # targets, and would invalidate the pinned root/object contract).
            if not (event == 'os.truncate' and args and isinstance(args[0], int)
                    and self._write_allowed(args[0], path)):
                self.denied_writes.append(event + ':' + str(path))
                raise PermissionError('evaluation_mutation_forbidden')
        if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind', 'socket.sendto'}:
            import traceback
            self.network_events.append({'event': event, 'admitted': self.provider_admitted,
                'trace': [f'{f.filename}:{f.name}:{f.lineno}' for f in traceback.extract_stack(limit=100) if not f.filename.startswith('<frozen')]})
            if not self.provider_admitted:
                raise PermissionError('evaluation_network_before_admission')
        if event in {'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
            raise PermissionError('evaluation_subprocess_forbidden')

    def install(self):
        sys.addaudithook(self.audit)

    def receipt(self):
        auth_reads = self.auth_reads  # Baseline verification itself reads auth.
        return {'baseline_unchanged': self.baseline == self._baseline() and self.environment == dict(os.environ),
                'denied_writes': list(self.denied_writes),
                'denied_private_reads': list(self.denied_private_reads),
                'network_events': list(self.network_events), 'auth_reads': auth_reads,
                'environment_baseline_sha256': hashlib.sha256(json.dumps(self.environment, sort_keys=True).encode()).hexdigest()}

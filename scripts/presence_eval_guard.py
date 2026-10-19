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


class ReadOnlyEvaluationGuard:
    def __init__(self, *, home, source, write_dirs):
        self.home = Path(home).absolute()
        self.source = Path(source).resolve()
        self.write_dirs = tuple(Path(p).resolve() for p in write_dirs)
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
            return Path(os.fsdecode(value)).absolute()
        return None

    def _within(self, path, roots):
        return path is not None and any(path == root or root in path.parents for root in roots)

    def audit(self, event, args):
        if event == 'open':
            path = self._path(args[0])
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and path != Path('/dev/null') and not self._within(path, self.write_dirs):
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
            if not self._within(path, self.write_dirs):
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

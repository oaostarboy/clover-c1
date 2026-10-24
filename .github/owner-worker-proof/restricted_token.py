"""Test-only, same-SID Windows limited-token launcher. No product imports.

SDK contracts: CreateRestrictedToken(DISABLE_MAX_PRIVILEGE | LUA_TOKEN),
CreateProcessAsUserW (restricted version of caller token needs no assign-primary
privilege), Get/SetTokenInformation. No registry/UAC/account/policy changes.
This is trusted fixture code, not a sandbox for hostile executables.
"""
import argparse
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ADMIN = 'S-1-5-32-544'
MEDIUM = 0x2000


def require_limited(value, sid, *, current=False):
    """Fail closed on absent evidence, elevation, enabled admins or wrong owner."""
    if (value['sid'] != sid or value['admin_member'] is not False
            or value['elevated'] is not False or value['integrity_rid'] != MEDIUM):
        raise RuntimeError('Not a same-owner medium non-admin token: ' + json.dumps(value))
    attributes = value['groups'].get(ADMIN, 0)
    if not attributes & 0x10 or attributes & 0x4:
        raise RuntimeError('Administrators SID must be deny-only, never enabled')
    if any(name != 'SeChangeNotifyPrivilege' for name in value['enabled_privileges']):
        raise RuntimeError('Unexpected enabled privilege')
    if (current and value['is_user_an_admin'] is not False) or value.get('is_user_an_admin') not in (None, False):
        raise RuntimeError('IsUserAnAdmin disagrees')


def child_environment(env):
    # Explicit allowlist: do not inherit GitHub runtime tokens or personal secrets.
    names = {'systemroot', 'windir', 'comspec', 'path', 'pathext', 'temp', 'tmp',
             'userprofile', 'homedrive', 'homepath', 'home', 'appdata', 'localappdata',
             'programdata', 'programfiles', 'programfiles(x86)', 'commonprogramfiles',
             'os', 'processor_architecture', 'number_of_processors', 'username',
             'userdomain', 'virtual_env', 'clover_home'}
    result = {k: v for k, v in env.items() if k.lower() in names}
    result.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONNOUSERSITE='1')
    return result


class SA(C.Structure):
    _fields_ = [('Sid', C.c_void_p), ('Attributes', W.DWORD)]


class TG(C.Structure):
    _fields_ = [('Count', W.DWORD), ('Groups', SA * 1)]


class LUID(C.Structure):
    _fields_ = [('LowPart', W.DWORD), ('HighPart', W.LONG)]


class LA(C.Structure):
    _fields_ = [('Luid', LUID), ('Attributes', W.DWORD)]


class TP(C.Structure):
    _fields_ = [('Count', W.DWORD), ('Privileges', LA * 1)]


class STARTUPINFO(C.Structure):
    _fields_ = [('cb', W.DWORD), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR),
                ('lpTitle', W.LPWSTR), ('dwX', W.DWORD), ('dwY', W.DWORD),
                ('dwXSize', W.DWORD), ('dwYSize', W.DWORD),
                ('dwXCountChars', W.DWORD), ('dwYCountChars', W.DWORD),
                ('dwFillAttribute', W.DWORD), ('dwFlags', W.DWORD),
                ('wShowWindow', W.WORD), ('cbReserved2', W.WORD),
                ('lpReserved2', C.c_void_p), ('hStdInput', W.HANDLE),
                ('hStdOutput', W.HANDLE), ('hStdError', W.HANDLE)]


class PROCESSINFO(C.Structure):
    _fields_ = [('hProcess', W.HANDLE), ('hThread', W.HANDLE),
                ('dwProcessId', W.DWORD), ('dwThreadId', W.DWORD)]


class WinAPI:
    def __init__(self):
        if sys.platform != 'win32':
            raise RuntimeError('Native Windows required')
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.a = C.WinDLL('advapi32', use_last_error=True)
        self.s = C.WinDLL('shell32', use_last_error=True)
        P, D, H, B = C.c_void_p, W.DWORD, W.HANDLE, W.BOOL
        self.bind(self.k, 'GetCurrentProcess', [], H)
        self.bind(self.k, 'CloseHandle', [H], B)
        self.bind(self.k, 'LocalFree', [P], P)
        self.bind(self.k, 'WaitForSingleObject', [H, D], D)
        self.bind(self.k, 'TerminateProcess', [H, W.UINT], B)
        self.bind(self.k, 'GetExitCodeProcess', [H, C.POINTER(D)], B)
        self.bind(self.a, 'OpenProcessToken', [H, D, C.POINTER(H)], B)
        self.bind(self.a, 'GetTokenInformation', [H, C.c_int, P, D, C.POINTER(D)], B)
        self.bind(self.a, 'SetTokenInformation', [H, C.c_int, P, D], B)
        self.bind(self.a, 'GetLengthSid', [P], D)
        self.bind(self.a, 'ConvertSidToStringSidW', [P, C.POINTER(P)], B)
        self.bind(self.a, 'ConvertStringSidToSidW', [W.LPCWSTR, C.POINTER(P)], B)
        self.bind(self.a, 'LookupPrivilegeNameW', [W.LPCWSTR, C.POINTER(LUID), W.LPWSTR, C.POINTER(D)], B)
        self.bind(self.a, 'DuplicateTokenEx', [H, D, P, C.c_int, C.c_int, C.POINTER(H)], B)
        self.bind(self.a, 'CheckTokenMembership', [H, P, C.POINTER(B)], B)
        self.bind(self.a, 'CreateRestrictedToken', [H, D, D, C.POINTER(SA), D, P, D, P, C.POINTER(H)], B)
        self.bind(self.a, 'CreateProcessAsUserW', [H, W.LPCWSTR, W.LPWSTR, P, P, B, D, P,
                  W.LPCWSTR, C.POINTER(STARTUPINFO), C.POINTER(PROCESSINFO)], B)
        self.bind(self.s, 'IsUserAnAdmin', [], B)

    @staticmethod
    def bind(dll, name, args, result):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result

    @staticmethod
    def check(ok, name):
        if not ok:
            error = C.get_last_error()
            raise OSError(error, name + ': ' + C.FormatError(error))

    @contextmanager
    def handle(self, value):
        try:
            yield value
        finally:
            if value:
                self.check(self.k.CloseHandle(value), 'CloseHandle')

    @contextmanager
    def sid(self, text):
        value = C.c_void_p()
        self.check(self.a.ConvertStringSidToSidW(text, C.byref(value)), 'ConvertStringSidToSidW')
        try:
            yield value
        finally:
            if self.k.LocalFree(value):
                raise RuntimeError('LocalFree SID failed')

    def sid_text(self, value):
        text = C.c_void_p()
        self.check(self.a.ConvertSidToStringSidW(value, C.byref(text)), 'ConvertSidToStringSidW')
        try:
            return C.wstring_at(text)
        finally:
            if self.k.LocalFree(text):
                raise RuntimeError('LocalFree SID string failed')

    def info(self, token, kind):
        length = W.DWORD()
        if self.a.GetTokenInformation(token, kind, None, 0, C.byref(length)):
            raise RuntimeError('Unexpected empty token information')
        error = C.get_last_error()
        if error != 122:  # ERROR_INSUFFICIENT_BUFFER, not an ignored API failure
            raise OSError(error, 'GetTokenInformation(size,class=' + str(kind) + '): ' + C.FormatError(error))
        buf = C.create_string_buffer(length.value)
        self.check(self.a.GetTokenInformation(token, kind, buf, length, C.byref(length)),
                   'GetTokenInformation(class=' + str(kind) + ')')
        return buf

    def elevation_info(self, token, kind):
        # SDK: TOKEN_ELEVATION is one DWORD; TOKEN_ELEVATION_TYPE is a
        # 32-bit enum. These fixed-size classes need no null-buffer size query.
        if kind not in (18, 20):
            raise ValueError('Not a fixed-size elevation information class')
        value, length = W.DWORD(), W.DWORD()
        size = C.sizeof(value)
        self.check(self.a.GetTokenInformation(token, kind, C.byref(value), size, C.byref(length)),
                   'GetTokenInformation(class=' + str(kind) + ')')
        if length.value != size:
            raise RuntimeError('Unexpected elevation information length: ' + str((kind, length.value, size)))
        return value.value

    @contextmanager
    def current(self):
        token = W.HANDLE()
        # QUERY | DUPLICATE | ASSIGN_PRIMARY | ADJUST_DEFAULT
        self.check(self.a.OpenProcessToken(self.k.GetCurrentProcess(), 0x8b, C.byref(token)), 'OpenProcessToken')
        with self.handle(token):
            yield token

    def snapshot(self, token, current=False):
        user = self.info(token, 1)
        groups = self.info(token, 2)
        count = C.cast(groups, C.POINTER(W.DWORD)).contents.value
        rows = (SA * count).from_address(C.addressof(groups) + TG.Groups.offset)
        integrity = self.info(token, 25)
        integrity_sid = self.sid_text(C.cast(integrity, C.POINTER(SA)).contents.Sid)
        privileges = self.info(token, 3)
        n = C.cast(privileges, C.POINTER(W.DWORD)).contents.value
        prows = (LA * n).from_address(C.addressof(privileges) + TP.Privileges.offset)
        enabled = []
        for row in prows:
            if row.Attributes & 2:
                size = W.DWORD(256)
                name = C.create_unicode_buffer(size.value)
                self.check(self.a.LookupPrivilegeNameW(None, C.byref(row.Luid), name, C.byref(size)), 'LookupPrivilegeNameW')
                enabled.append(name.value)
        member = W.BOOL()
        impersonation = W.HANDLE()
        self.check(self.a.DuplicateTokenEx(token, 0xc, None, 2, 2, C.byref(impersonation)), 'DuplicateTokenEx')
        with self.handle(impersonation), self.sid(ADMIN) as admin:
            self.check(self.a.CheckTokenMembership(impersonation, admin, C.byref(member)), 'CheckTokenMembership')
        return dict(sid=self.sid_text(C.cast(user, C.POINTER(SA)).contents.Sid),
                    groups={self.sid_text(r.Sid): r.Attributes for r in rows},
                    elevated=bool(self.elevation_info(token, 20)),
                    elevation_type=self.elevation_info(token, 18),
                    integrity_sid=integrity_sid, integrity_rid=int(integrity_sid.rsplit('-', 1)[1]),
                    admin_member=bool(member), enabled_privileges=enabled,
                    is_user_an_admin=bool(self.s.IsUserAnAdmin()) if current else None,
                    userprofile=os.environ.get('USERPROFILE'), home=os.environ.get('HOME'),
                    clover_home=os.environ.get('CLOVER_HOME'))

    @contextmanager
    def limited(self, token):
        # Prefer the OS-linked filtered token when present. Built-in Administrator
        # often has no linked token; never infer or alter machine UAC policy.
        linked = W.HANDLE()
        size = W.DWORD()
        ok = self.a.GetTokenInformation(token, 19, C.byref(linked), C.sizeof(linked), C.byref(size))
        if not ok and C.get_last_error() != 1312:  # ERROR_NO_SUCH_LOGON_SESSION
            self.check(False, 'GetTokenInformation(TokenLinkedToken)')
        with self.handle(linked):
            restricted = W.HANDLE()
            with self.sid(ADMIN) as admin:
                disabled = SA(admin, 0)
                self.check(self.a.CreateRestrictedToken(linked if ok else token, 0x1 | 0x4,
                           1, C.byref(disabled), 0, None, 0, None, C.byref(restricted)), 'CreateRestrictedToken')
            with self.handle(restricted):
                with self.sid('S-1-16-8192') as medium:
                    label = SA(medium, 0x20)  # SE_GROUP_INTEGRITY
                    self.check(self.a.SetTokenInformation(restricted, 25, C.byref(label),
                               C.sizeof(label) + self.a.GetLengthSid(medium)), 'SetTokenInformation(TokenIntegrityLevel)')
                yield restricted, 'linked-filtered-then-restricted' if ok else 'current-LUA-restricted'

    def spawn(self, token, argv, cwd, env):
        startup = STARTUPINFO()
        startup.cb = C.sizeof(startup)
        process = PROCESSINFO()
        command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
        block = C.create_unicode_buffer('\0'.join(k + '=' + v for k, v in sorted(env.items(), key=lambda x: x[0].upper())) + '\0\0')
        # No inherited handles, no console, explicit Unicode environment.
        self.check(self.a.CreateProcessAsUserW(token, argv[0], command, None, None, False,
                   0x08000000 | 0x400, block, str(cwd), C.byref(startup), C.byref(process)), 'CreateProcessAsUserW')
        with self.handle(process.hProcess), self.handle(process.hThread):
            result = self.k.WaitForSingleObject(process.hProcess, 120000)
            if result != 0:
                error = C.get_last_error() if result == 0xffffffff else None
                # Exact retained child handle/PID, never a name or global sweep.
                killer = str(Path(os.environ['SystemRoot']) / 'System32' / 'taskkill.exe')
                cleanup = subprocess.run([killer, '/PID', str(process.dwProcessId), '/T', '/F'],
                                         capture_output=True, timeout=10, check=False)
                if cleanup.returncode != 0:
                    self.check(self.k.TerminateProcess(process.hProcess, 124), 'TerminateProcess(owned child)')
                    raise RuntimeError('Owned subtree cleanup failed: ' + str(cleanup.returncode))
                self.k.WaitForSingleObject(process.hProcess, 10000)
                raise RuntimeError('Owned token child wait failed/timed out: ' + str((result, error)))
            code = W.DWORD()
            self.check(self.k.GetExitCodeProcess(process.hProcess, C.byref(code)), 'GetExitCodeProcess')
            return code.value, process.dwProcessId


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    separator = argv.index('--') if '--' in argv else len(argv)
    command = argv[separator + 1:]
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['identity', 'launch', 'child'])
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('--parent-sid')
    a = parser.parse_args(argv[:separator])
    a.command = command
    api = WinAPI()
    with api.current() as token:
        identity = api.snapshot(token, current=True)
        if a.mode == 'identity':
            dump(a.evidence, identity)
            return 0
        if not a.cwd or not a.command or not Path(a.command[0]).is_absolute():
            raise ValueError('Explicit cwd and -- absolute executable argv required')
        if a.mode == 'child':
            require_limited(identity, a.parent_sid, current=True)
            dump(a.evidence / 'token-child.json', identity)
            with (a.evidence / 'token-command.log').open('wb') as log:
                return subprocess.run(a.command, cwd=a.cwd, stdout=log, stderr=subprocess.STDOUT,
                                      check=False).returncode
        if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
            raise RuntimeError('Disposable hosted runner required')
        if any((a.evidence / name).exists() for name in ('token-parent.json', 'token-child.json', 'token-launch.json')):
            raise RuntimeError('Refuse stale token evidence')
        dump(a.evidence / 'token-parent.json', identity)
        with api.limited(token) as (limited, model):
            selected = api.snapshot(limited)
            dump(a.evidence / 'token-selected.json', dict(model=model, **selected))
            require_limited(selected, identity['sid'])
            command = [sys.executable, '-I', str(Path(__file__).resolve()), 'child',
                       '--evidence', str(a.evidence), '--cwd', str(a.cwd),
                       '--parent-sid', identity['sid'], '--', *a.command]
            code, pid = api.spawn(limited, command, a.cwd, child_environment(os.environ))
        dump(a.evidence / 'token-launch.json', dict(exit=code, pid=pid, model=model, python=sys.executable,
             helper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
        observed = json.loads((a.evidence / 'token-child.json').read_text(encoding='utf-8'))
        require_limited(observed, identity['sid'], current=True)
        for name in ('userprofile', 'home', 'clover_home'):
            if identity[name] != observed[name]:
                raise RuntimeError('Child changed profile/home: ' + name)
        return code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        error = dict(error_type=type(exc).__name__, error=str(exc), errno=getattr(exc, 'errno', None))
        if '--evidence' in sys.argv:
            target = Path(sys.argv[sys.argv.index('--evidence') + 1])
            directory = target if target.is_dir() else target.parent
            if directory.is_dir():
                dump(directory / ('token-error-' + sys.argv[1] + '.json'), error)
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)

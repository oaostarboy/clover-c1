"""Read-only native token observation for a real disposable account.

Historical filename retained for review continuity. No token manufacturing or
process launch APIs remain. The OS credential/profile launcher owns logon.
"""
import argparse
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import sys

ADMIN = 'S-1-5-32-544'
MEDIUM = 0x2000


def require_limited(value, sid, *, current=False):
    """Require the created account SID, not the hosted runner's SID."""
    if (value['sid'] != sid or value['admin_member'] is not False
            or value['elevated'] is not False or value['integrity_rid'] != MEDIUM):
        raise RuntimeError('Not the intended medium non-admin account')
    if ADMIN in value['groups'] or not value['groups'].get('S-1-5-32-545', 0) & 4:
        raise RuntimeError('Require Users membership and NO Administrators SID')
    if any(name != 'SeChangeNotifyPrivilege' for name in value['enabled_privileges']):
        raise RuntimeError('Unexpected enabled privilege')
    if (current and value['is_user_an_admin'] is not False) or value.get('is_user_an_admin') not in (None, False):
        raise RuntimeError('IsUserAnAdmin disagrees')


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
        self.bind(self.a, 'OpenProcessToken', [H, D, C.POINTER(H)], B)
        self.bind(self.a, 'GetTokenInformation', [H, C.c_int, P, D, C.POINTER(D)], B)
        self.bind(self.a, 'ConvertSidToStringSidW', [P, C.POINTER(P)], B)
        self.bind(self.a, 'ConvertStringSidToSidW', [W.LPCWSTR, C.POINTER(P)], B)
        self.bind(self.a, 'LookupPrivilegeNameW', [W.LPCWSTR, C.POINTER(LUID), W.LPWSTR, C.POINTER(D)], B)
        self.bind(self.a, 'DuplicateTokenEx', [H, D, P, C.c_int, C.c_int, C.POINTER(H)], B)
        self.bind(self.a, 'CheckTokenMembership', [H, P, C.POINTER(B)], B)
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
        # QUERY | DUPLICATE only: read-only observation
        self.check(self.a.OpenProcessToken(self.k.GetCurrentProcess(), 0x0a, C.byref(token)), 'OpenProcessToken')
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


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-sid', required=True)
    a = parser.parse_args(argv)
    api = WinAPI()
    with api.current() as token:
        observed = api.snapshot(token, current=True)
    dump(a.evidence, observed)
    require_limited(observed, a.expected_sid, current=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

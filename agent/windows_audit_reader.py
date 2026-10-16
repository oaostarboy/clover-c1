"""Fail-closed, local-drive Windows audit reads using directory-relative handles.

Never resolve a pathname and then reopen it. Each literal component is opened
relative to its retained parent with NtCreateFile(OBJ_DONT_REPARSE), and every
handle refuses write/delete sharing. All ancestors stay pinned through the read.
Only disk regular files are accepted; no UNC/device paths, ADS or DOS aliases.
No Windows API is loaded on other hosts. Callers retain their authority checks.
"""
from __future__ import annotations

import re

_RESERVED = re.compile(r'^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)


def _component(value: str) -> bool:
    return bool(value and value not in {'.', '..'} and not value.endswith((' ', '.'))
                and not _RESERVED.match(value)
                and not any(ord(c) < 32 or c in '\\/:*?"<>|' for c in value))


def validated_parts(project: str, name: str, limit: int) -> tuple[str, tuple[str, ...]]:
    """Reject namespaces/aliases before I/O; do not silently normalize inputs."""
    if (type(project) is not str or type(name) is not str or type(limit) is not int
            or limit < 0 or limit > 4 * 1024 * 1024
            or not re.match(r'^[A-Za-z]:[\\/]', project)):
        raise ValueError('nonlocal Windows audit path')
    root = project[:2] + '\\'
    tail = project[3:].replace('\\', '/')
    # A drive root is valid, but empty internal or trailing components are not.
    parents = tuple(tail.split('/')) if tail else ()
    parts = tuple(name.split('/'))
    if not parts or not all(_component(p) for p in (*parents, *parts)):
        raise ValueError('nonliteral Windows audit component')
    return root, (*parents, *parts)


def _api():
    import ctypes as c
    from ctypes import wintypes as w

    class UnicodeString(c.Structure):
        _fields_ = [('Length', w.USHORT), ('MaximumLength', w.USHORT), ('Buffer', w.LPWSTR)]

    class ObjectAttributes(c.Structure):
        _fields_ = [('Length', w.ULONG), ('RootDirectory', w.HANDLE),
                    ('ObjectName', c.POINTER(UnicodeString)), ('Attributes', w.ULONG),
                    ('SecurityDescriptor', w.LPVOID), ('SecurityQualityOfService', w.LPVOID)]

    class IoStatus(c.Structure):
        _fields_ = [('Status', c.c_void_p), ('Information', c.c_size_t)]

    class FileInfo(c.Structure):
        _fields_ = [('Attributes', w.DWORD), ('Creation', w.FILETIME),
                    ('Access', w.FILETIME), ('Write', w.FILETIME), ('Volume', w.DWORD),
                    ('SizeHigh', w.DWORD), ('SizeLow', w.DWORD), ('Links', w.DWORD),
                    ('IndexHigh', w.DWORD), ('IndexLow', w.DWORD)]

    class BasicInfo(c.Structure):
        _fields_ = [('Creation', c.c_longlong), ('Access', c.c_longlong),
                    ('Write', c.c_longlong), ('Change', c.c_longlong), ('Attributes', w.DWORD)]

    kernel = c.WinDLL('kernel32', use_last_error=True)
    nt = c.WinDLL('ntdll')
    nt.NtCreateFile.argtypes = [c.POINTER(w.HANDLE), w.ULONG, c.POINTER(ObjectAttributes),
        c.POINTER(IoStatus), c.c_void_p, w.ULONG, w.ULONG, w.ULONG, w.ULONG, c.c_void_p, w.ULONG]
    nt.NtCreateFile.restype = w.LONG
    nt.RtlNtStatusToDosError.argtypes = [w.LONG]
    nt.RtlNtStatusToDosError.restype = w.ULONG
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.GetFileType.argtypes = [w.HANDLE]
    kernel.GetFileType.restype = w.DWORD
    kernel.GetFileInformationByHandle.argtypes = [w.HANDLE, c.POINTER(FileInfo)]
    kernel.GetFileInformationByHandle.restype = w.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD]
    kernel.GetFileInformationByHandleEx.restype = w.BOOL
    kernel.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID]
    kernel.ReadFile.restype = w.BOOL

    class Api:
        def open(self, parent, part, directory):
            buf = c.create_unicode_buffer(part)
            length = len(part.encode('utf-16-le'))
            if length > 65532:
                raise ValueError('Windows audit component too long')
            name = UnicodeString(length, length + 2, c.cast(buf, w.LPWSTR))
            attrs = ObjectAttributes(c.sizeof(ObjectAttributes), parent, c.pointer(name),
                                     0x40 | 0x1000, None, None)  # CASE_INSENSITIVE | DONT_REPARSE
            handle, status = w.HANDLE(), IoStatus()
            # READ_ATTRIBUTES | SYNCHRONIZE | (TRAVERSE or READ_DATA).
            access = 0x80 | 0x100000 | (0x20 if directory else 1)
            options = 0x200000 | 0x20 | (1 if directory else 0x40)
            result = nt.NtCreateFile(c.byref(handle), access, c.byref(attrs), c.byref(status),
                                     None, 0, 1, 1, options, None, 0)
            # FILE_SHARE_READ only; FILE_OPEN; OPEN_REPARSE_POINT; SYNCHRONOUS_IO_NONALERT.
            if result < 0:
                raise c.WinError(nt.RtlNtStatusToDosError(result))
            return handle

        def snapshot(self, handle, directory):
            info, basic = FileInfo(), BasicInfo()
            if (not kernel.GetFileInformationByHandle(handle, c.byref(info))
                    or not kernel.GetFileInformationByHandleEx(handle, 0, c.byref(basic), c.sizeof(basic))):
                raise c.WinError(c.get_last_error())
            if (kernel.GetFileType(handle) != 1 or info.Attributes & 0x400
                    or bool(info.Attributes & 0x10) != directory):
                raise ValueError('audit handle is not plain disk data')
            size = (info.SizeHigh << 32) | info.SizeLow
            return (info.Volume, info.IndexHigh, info.IndexLow, size,
                    basic.Write, basic.Change, info.Attributes, info.Links)

        def read(self, handle, size):
            buf, count = c.create_string_buffer(size), w.DWORD()
            if not kernel.ReadFile(handle, buf, size, c.byref(count), None):
                raise c.WinError(c.get_last_error())
            return buf.raw[:count.value]

        def close(self, handle):
            kernel.CloseHandle(handle)

    return Api()


def read_bytes(project: str, name: str, limit: int) -> bytes:
    import sys
    if sys.platform != 'win32':
        raise OSError('native Windows audit reader required')
    root, parts = validated_parts(project, name, limit)
    api = _api()
    handles = []
    try:
        handle = api.open(None, '\\??\\' + root, True)
        handles.append(handle)
        directories = [(handle, api.snapshot(handle, True))]
        for part in parts[:-1]:
            handle = api.open(handle, part, True)
            handles.append(handle)
            directories.append((handle, api.snapshot(handle, True)))
        leaf = api.open(handle, parts[-1], False)
        handles.append(leaf)
        before = api.snapshot(leaf, False)
        if before[3] > limit:
            raise ValueError('audit file exceeds size bound')
        value = api.read(leaf, limit + 1)
        if len(value) != before[3] or len(value) > limit or api.snapshot(leaf, False) != before:
            raise ValueError('audit file changed during read')
        # The handles pin the namespace; metadata also detects in-place changes.
        for directory, initial in directories:
            if api.snapshot(directory, True) != initial:
                raise ValueError('audit directory changed during read')
        return value
    finally:
        for handle in reversed(handles):
            api.close(handle)

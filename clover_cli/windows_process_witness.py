"""Read-only Win32 process witness, executed only in isolated base Python.

Keep ctypes out of the updater that replaces venv extension modules. This
script reports OS measurements; the receipt owner still validates all lineage,
creation-time, executable and source predicates independently.
"""
from __future__ import annotations


def chain_from_snapshot(pid, parents, observe):
    if pid not in parents:
        raise ValueError('missing current process in snapshot')
    chain, seen = [], set()
    for _ in range(8):
        if pid <= 0 or pid not in parents:
            break
        if pid in seen:
            raise ValueError('cyclic process ancestry')
        seen.add(pid)
        try:
            facts = observe(pid)
        except OSError as exc:
            if not chain:
                raise
            # Unknown ancestors remain explicitly unknown. The caller rejects
            # one on an ownership edge; an unrelated higher ancestor need not
            # be inspectable. Never invent an executable or birth timestamp.
            facts = dict(executable=None, created_utc=None, observation_error=type(exc).__name__)
            chain.append(dict(pid=pid, parent_pid=parents[pid], **facts))
            break
        chain.append(dict(pid=pid, parent_pid=parents[pid], **facts))
        pid = parents[pid]
    return chain


def native_chain(pid):
    import sys
    if sys.platform != 'win32':
        raise RuntimeError('native Windows process witness required')
    import ctypes
    from ctypes import wintypes as w
    from datetime import datetime, timezone

    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ProcessID', w.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', w.DWORD),
                    ('cntThreads', w.DWORD), ('th32ParentProcessID', w.DWORD),
                    ('pcPriClassBase', w.LONG), ('dwFlags', w.DWORD), ('szExeFile', w.WCHAR * 260)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateToolhelp32Snapshot': ([w.DWORD, w.DWORD], w.HANDLE),
        'Process32FirstW': ([w.HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL),
        'Process32NextW': ([w.HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL),
        'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        'GetProcessTimes': ([w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)], w.BOOL),
        'QueryFullProcessImageNameW': ([w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
        'CloseHandle': ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        parents = {}
        while True:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise ctypes.WinError(ctypes.get_last_error())
                break
    finally:
        kernel.CloseHandle(snapshot)

    def observe(current):
        handle = kernel.OpenProcess(0x1000, False, current)  # QUERY_LIMITED_INFORMATION
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            created, exited, system, user = (w.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(system), ctypes.byref(user)):
                raise ctypes.WinError(ctypes.get_last_error())
            size = w.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            stamp = datetime.fromtimestamp(ticks / 10_000_000 - 11_644_473_600, timezone.utc).isoformat()
            return dict(executable=buffer.value, created_utc=stamp)
        finally:
            kernel.CloseHandle(handle)

    return chain_from_snapshot(pid, parents, observe)


if __name__ == '__main__':
    import json
    import sys
    if len(sys.argv) != 2 or not sys.argv[1].isascii() or not sys.argv[1].isdigit():
        raise ValueError('one literal process ID is required')
    pid = int(sys.argv[1])
    if not 0 < pid <= 0xFFFFFFFF:
        raise ValueError('process ID outside Win32 range')
    print(json.dumps(native_chain(pid), ensure_ascii=True))

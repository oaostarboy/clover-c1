"""Wake only this private helper's two locked native reader/cache threads.

No global names, foreign process threads, guessed handles or process termination.
CancelSynchronousIo runs only after the terminal child exited naturally; cache
reads must still drain to the binding's explicit EOF exception before success.
"""
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import sys


class ReaderWake:
    def __init__(self, native_type):
        self.handles = []
        self.calls = self.cancelled = 0
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.nt = ctypes.WinDLL('ntdll')
        self.psapi = ctypes.WinDLL('psapi', use_last_error=True)
        signatures = {
            'GetCurrentProcess': ([], w.HANDLE),
            'GetModuleHandleW': ([w.LPCWSTR], w.HMODULE),
            'GetCurrentProcessId': ([], w.DWORD),
            'CreateToolhelp32Snapshot': ([w.DWORD, w.DWORD], w.HANDLE),
            'OpenThread': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'CancelSynchronousIo': ([w.HANDLE], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result
        class ThreadEntry(ctypes.Structure):
            _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD),
                        ('th32ThreadID', w.DWORD), ('th32OwnerProcessID', w.DWORD),
                        ('tpBasePri', w.LONG), ('tpDeltaPri', w.LONG), ('dwFlags', w.DWORD)]
        class ModuleInfo(ctypes.Structure):
            _fields_ = [('base', w.LPVOID), ('size', w.DWORD), ('entry', w.LPVOID)]
        for name in ('Thread32First', 'Thread32Next'):
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = [w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL
        self.psapi.GetModuleInformation.argtypes = [w.HANDLE, w.HMODULE, ctypes.POINTER(ModuleInfo), w.DWORD]
        self.psapi.GetModuleInformation.restype = w.BOOL
        self.nt.NtQueryInformationThread.argtypes = [w.HANDLE, w.ULONG, w.LPVOID, w.ULONG, w.LPVOID]
        self.nt.NtQueryInformationThread.restype = w.LONG
        native = sys.modules['winpty.winpty']
        if native.PTY is not native_type:
            raise RuntimeError('locked native thread module identity mismatch')
        module_path = Path(native.__file__).resolve()
        module = self.k.GetModuleHandleW(str(module_path))
        info = ModuleInfo()
        if not module or not self.psapi.GetModuleInformation(self.k.GetCurrentProcess(), module,
                                                           ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.module_name = module_path.name
        snapshot = self.k.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            row = ThreadEntry()
            row.dwSize = ctypes.sizeof(row)
            more = self.k.Thread32First(snapshot, ctypes.byref(row))
            if not more:
                raise ctypes.WinError(ctypes.get_last_error())
            while more:
                if row.th32OwnerProcessID == os.getpid():
                    handle = self.k.OpenThread(0x0040 | 0x0001, False, row.th32ThreadID)
                    if not handle:
                        raise ctypes.WinError(ctypes.get_last_error())
                    keep = False
                    try:
                        address = ctypes.c_void_p()
                        status = self.nt.NtQueryInformationThread(handle, 9, ctypes.byref(address),
                                                                 ctypes.sizeof(address), None)
                        if status != 0:
                            raise RuntimeError('owned thread start-address query failed')
                        keep = address.value is not None and info.base <= address.value < info.base + info.size
                        if keep:
                            self.handles.append(handle)
                    finally:
                        if not keep:
                            self.k.CloseHandle(handle)
                more = self.k.Thread32Next(snapshot, ctypes.byref(row))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(ctypes.get_last_error())
            if len(self.handles) != 2:
                raise RuntimeError('locked native reader/cache thread ownership not established')
        except BaseException:
            self.close()
            raise
        finally:
            self.k.CloseHandle(snapshot)
        # Native windows::Error.message() uses the OS error text. Never accept a
        # generic read exception or an error while the terminal child is alive.
        self.wake_errors = set()
        for code in (995, 109):
            text = ctypes.FormatError(code).strip()
            self.wake_errors.update((text, text + f' (0x{0x80070000 | code:08X})'))

    def wake_after_exit(self):
        self.calls += 1
        for handle in self.handles:
            if self.k.CancelSynchronousIo(handle):
                self.cancelled += 1
            elif ctypes.get_last_error() != 1168:  # ERROR_NOT_FOUND: no pending synchronous IO
                raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        handles, self.handles = self.handles, []
        error = None
        for handle in handles:
            if not self.k.CloseHandle(handle):
                error = ctypes.WinError(ctypes.get_last_error())
        if error is not None:
            raise error

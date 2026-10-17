"""Diagnostic-only sitecustomize, installed untracked then hash-removed.
No arguments, env values, frame locals, input, output or return values logged.
"""
import sys

if 'pairing' in sys.argv and 'owner-confirm' in sys.argv:
    import atexit
    import ctypes
    import faulthandler
    import json
    import os
    from pathlib import Path
    import threading
    import time

    # Written by the driver; no child environment changes are necessary.
    _out = Path(__file__).with_name('owner_cli_observer_destination.txt').read_text().strip()
    _events = open(str(Path(_out) / ('child-' + str(os.getpid()) + '.jsonl')), 'a', encoding='utf-8', buffering=1)
    _stack = open(str(Path(_out) / ('child-' + str(os.getpid()) + '.stacks.txt')), 'a', encoding='utf-8', buffering=1)
    _start = time.monotonic()

    def emit(event, **fields):
        _events.write(json.dumps({'event': event, 'seconds': time.monotonic()-_start, 'pid': os.getpid(), 'thread': threading.get_ident(), **fields}) + '\n')

    def handles():
        result = {}
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
            kernel.GetStdHandle.restype = ctypes.c_void_p
            kernel.GetFileType.argtypes = [ctypes.c_void_p]
            kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            for name, number in [('stdin', -10), ('stdout', -11), ('stderr', -12)]:
                handle = kernel.GetStdHandle(number & 0xffffffff)
                mode = ctypes.c_ulong()
                is_console = bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)))
                result[name] = {'file_type': kernel.GetFileType(handle), 'console_mode_available': is_console, 'console_mode': mode.value if is_console else None}
        return result

    emit('observer_start', stdin_isatty=sys.stdin.isatty(), stdout_isatty=sys.stdout.isatty(), stdin_is_original=sys.stdin is sys.__stdin__, std_handles=handles(), python=sys.version, argv_shape='-m clover_cli.main pairing owner-confirm telegram', ppid=os.getppid())
    faulthandler.dump_traceback_later(10, repeat=True, file=_stack)
    _main_functions = {'<module>', '_apply_profile_override', 'main', '_run_and_exit_oneshot', '_cleanup_oneshot_runtime', '_exit_after_oneshot'}
    _other_functions = {'get_clover_home', 'get_default_clover_root', 'load_config', 'ensure_windows_bin_launchers', 'pairing_command', 'telegram_owner_command', 'prompt', '_owner_lock', 'confirm', '_load_unlocked', '_save_unlocked', '__init__', 'close'}
    _seen = set()

    def observe(frame, event, arg):
        if event not in ('call', 'return'):
            return
        filename = frame.f_code.co_filename.replace('\\', '/')
        name = frame.f_code.co_name
        interesting = (filename.endswith('/getpass.py') or
                       filename.endswith('/clover_bootstrap.py') or
                       filename.endswith('/clover_cli/main.py') and name in _main_functions or
                       any(filename.endswith('/' + p) for p in ['clover_cli/config.py', 'clover_cli/pairing.py', 'clover_cli/setup.py', 'clover_constants.py', 'clover_cli/install.py', 'gateway/pairing.py', 'clover_state.py']) and name in _other_functions)
        if interesting:
            key = (filename, name, event)
            # Limit repeated helper calls; values are deliberately never inspected.
            if key not in _seen:
                _seen.add(key)
                emit('phase', action=event, file=filename, function=name, line=frame.f_code.co_firstlineno)
    sys.setprofile(observe)

    def finish():
        sys.setprofile(None)
        faulthandler.cancel_dump_traceback_later()
        emit('atexit', threads=[{'name': t.name, 'daemon': t.daemon, 'alive': t.is_alive()} for t in threading.enumerate()])
        _stack.close()
        _events.close()
    atexit.register(finish)

"""Native-only test adapters; no production behavior or policy overrides."""
import ipaddress
import os
import socket
import subprocess
import sys
import threading


def run_python_cli(python, script, arguments, *, tty, env, timeout):
    """Give the fixture actual terminal stdin, not an isatty monkeypatch.

    Windows has no POSIX pty module. A hidden new native console supplies
    CONIN$ for real character-device stdin while stdout/stderr remain captured.
    """
    command = [python, '-c', script, *arguments]
    options = {'stdin': subprocess.PIPE, 'capture_output': True, 'text': True,
               'encoding': 'utf-8', 'timeout': timeout,
               'env': dict(env, PYTHONIOENCODING='utf-8')}
    if not tty:
        return subprocess.run(command, **options)
    if sys.platform == 'win32':
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        command[2] = "import sys; sys.stdin = open('CONIN$', 'r', encoding='utf-8')\n" + script
        options.update(creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        return subprocess.run(command, **options)
    import pty
    master, slave = pty.openpty()
    try:
        options['stdin'] = slave
        return subprocess.run(command, **options)
    finally:
        os.close(slave)
        os.close(master)


def private_socketpair_boundary(original_pair, original_connect, refuse):
    """Allow only synchronous stdlib socketpair's private loopback connection.

    CPython on Windows implements socketpair via its own two TCP sockets.
    Arbitrary application loopback, DNS, connect_ex and create_connection stay
    refused. The factory accepts no destination address; another thread never
    inherits its narrow synchronous allowance. No application await/callback
    executes inside the cached stdlib factory.
    """
    local = threading.local()
    factory_code = getattr(original_pair, '__code__', None)

    def pair(*args, **kwargs):
        if (len(args) > 3 or set(kwargs) - {'family', 'type', 'proto'}
                or any(value is not None and type(value) not in
                       (int, socket.AddressFamily, socket.SocketKind)
                       for value in (*args, *kwargs.values()))):
            raise TypeError('private socketpair accepts only primitive numeric arguments')
        previous = getattr(local, 'inside_pair', False)
        local.inside_pair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            local.inside_pair = previous

    def connect(sock, address):
        allowed = False
        if (getattr(local, 'inside_pair', False)
                and factory_code is not None and sys._getframe(1).f_code is factory_code
                and sock.family in (socket.AF_INET, socket.AF_INET6)
                and sock.type == socket.SOCK_STREAM
                and isinstance(address, tuple) and len(address) in (2, 4)
                and isinstance(address[0], str)
                and type(address[1]) is int and 0 < address[1] < 65536):
            try:
                allowed = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                pass
        if allowed:
            return original_connect(sock, address)
        return refuse(sock, address)

    return pair, connect

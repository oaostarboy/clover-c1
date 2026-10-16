"""Platform test boundaries: actual private TCP/TTY I/O, no external services."""
import os
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('tty', [False, True])
def test_windows_cli_fixture_uses_native_console_not_posix_pty(tmp_path, monkeypatch, tty):
    from tests.cron.test_workstream_state import TestCliStateWritesDisabled
    calls = []
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setitem(sys.modules, 'pty', None)
    monkeypatch.setattr(subprocess, 'STARTUPINFO', lambda: SimpleNamespace(dwFlags=0, wShowWindow=1), raising=False)
    monkeypatch.setattr(subprocess, 'STARTF_USESHOWWINDOW', 1, raising=False)
    monkeypatch.setattr(subprocess, 'SW_HIDE', 0, raising=False)
    monkeypatch.setattr(subprocess, 'CREATE_NEW_CONSOLE', 16, raising=False)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)) or subprocess.CompletedProcess(args[0], 0, '', ''))
    world = {'job': {'id': 'synthetic-fixture'}, 'db': tmp_path/'db', 'home': tmp_path}
    TestCliStateWritesDisabled()._run(world, ['cron', 'workstream'], pty_stdin=tty)
    (args, kwargs), = calls
    assert kwargs['capture_output'] is True and kwargs['timeout'] == 120
    assert kwargs.get('shell', False) is False
    if tty:
        assert kwargs['creationflags'] == subprocess.CREATE_NEW_CONSOLE
        assert kwargs['startupinfo'].wShowWindow == subprocess.SW_HIDE
        assert 'CONIN$' in args[0][2]
        assert 'isatty =' not in args[0][2]
    else:
        assert kwargs['stdin'] == subprocess.PIPE
        assert 'creationflags' not in kwargs


def _tcp_pair():
    """Same private loopback protocol as CPython's Windows socketpair fallback."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client.connect(listener.getsockname())
        server, _ = listener.accept()
        return client, server
    except BaseException:
        client.close()
        raise
    finally:
        listener.close()


def test_guard_accepts_only_its_private_socketpair_ipc(monkeypatch):
    from tests.gateway.test_trusted_human_origin_ingress import network_guard
    monkeypatch.setattr(socket, 'socketpair', _tcp_pair)
    guard = network_guard.__wrapped__(monkeypatch)
    attempts = next(guard)
    left = right = None
    try:
        left, right = socket.socketpair()
        left.sendall(b'private-self-pipe')
        assert right.recv(64) == b'private-self-pipe'
        assert attempts == []
    finally:
        if left is not None: left.close()
        if right is not None: right.close()
    with pytest.raises(StopIteration): next(guard)


@pytest.mark.parametrize('route', ['connect', 'connect_ex', 'create_connection', 'dns'])
def test_guard_still_refuses_application_loopback_and_dns(monkeypatch, route):
    from tests.gateway.test_trusted_human_origin_ingress import network_guard
    guard = network_guard.__wrapped__(monkeypatch)
    attempts = next(guard)
    sock = socket.socket()
    try:
        with pytest.raises(OSError, match='network attempt refused'):
            if route == 'connect': sock.connect(('127.0.0.1', 9))
            elif route == 'connect_ex': sock.connect_ex(('127.0.0.1', 9))
            elif route == 'create_connection': socket.create_connection(('127.0.0.1', 9))
            else: socket.getaddrinfo('example.invalid', 443)
        assert len(attempts) == 1
        with pytest.raises(AssertionError, match='unexpected network attempts'): next(guard)
    finally:
        sock.close()


def test_real_tty_and_pipe_are_distinct_without_mocking_isatty():
    from tests.native_boundaries import run_python_cli
    script = 'import sys; print("TTY", sys.stdin.isatty())'
    environment = dict(os.environ)
    tty = run_python_cli(sys.executable, script, [], tty=True, env=environment, timeout=120)
    pipe = run_python_cli(sys.executable, script, [], tty=False, env=environment, timeout=120)
    assert tty.returncode == pipe.returncode == 0
    assert 'TTY True' in tty.stdout
    assert 'TTY False' in pipe.stdout


def test_socketpair_allowance_cannot_escape_through_an_application_callback(monkeypatch):
    from tests.native_boundaries import private_socketpair_boundary
    attempts = []
    def callback():
        with socket.socket() as sock:
            sock.connect(('127.0.0.1', 9))
    def factory():
        callback()
    def must_not_connect(*_args):
        raise AssertionError('escaped private IPC boundary')
    def refuse(*_args):
        attempts.append('connect')
        raise OSError('network attempt refused')
    pair, connect = private_socketpair_boundary(factory, must_not_connect, refuse)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    with pytest.raises(OSError, match='network attempt refused'):
        pair()
    assert attempts == ['connect']

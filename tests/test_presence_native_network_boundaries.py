"""Offline guard contracts for native event-loop IPC and refused network I/O."""
import socket

import pytest

from tests.test_native_boundaries import _tcp_pair
from tests import test_presence_surface_wire as surface_wire


def test_w3_guard_allows_only_stdlib_private_socketpair(monkeypatch):
    # Exercise the Windows stdlib protocol on any host without faking its OS.
    monkeypatch.setattr(socket, 'socketpair', _tcp_pair)
    surface_wire.block_real_network.__wrapped__(monkeypatch)
    left, right = socket.socketpair()
    try:
        left.sendall(b'private event-loop wakeup')
        assert right.recv(64) == b'private event-loop wakeup'
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize('route', ['connect', 'connect_ex', 'create_connection', 'dns'])
def test_w3_guard_refuses_application_loopback_and_external_dns(monkeypatch, route):
    surface_wire.block_real_network.__wrapped__(monkeypatch)
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match='must not use real networking'):
            if route == 'connect':
                sock.connect(('127.0.0.1', 9))
            elif route == 'connect_ex':
                sock.connect_ex(('127.0.0.1', 9))
            elif route == 'create_connection':
                socket.create_connection(('127.0.0.1', 9))
            else:
                socket.getaddrinfo('example.invalid', 443)

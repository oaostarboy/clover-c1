"""Offline regression guard: synthetic loopback HTTP only, no external network.

Load with pytest -p qa.presence.network_guard. Existing wire-invariant tests use
real local HTTP servers; native SDK MockTransport tests need no sockets.
"""
import ipaddress
import socket
import pytest


def _local(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def presence_offline_network_guard(monkeypatch):
    resolve, create = socket.getaddrinfo, socket.create_connection
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def check(address):
        if not isinstance(address, tuple) or not _local(address[0]):
            raise AssertionError('external networking is forbidden in presence integration regression')
    def local_resolve(host, *args, **kwargs):
        if not _local(host):
            raise AssertionError('external DNS is forbidden in presence integration regression')
        return resolve(host, *args, **kwargs)
    def local_create(address, *args, **kwargs):
        check(address)
        return create(address, *args, **kwargs)
    def local_connect(sock, address):
        if sock.family != getattr(socket, 'AF_UNIX', None):
            check(address)
        return connect(sock, address)
    def local_connect_ex(sock, address):
        if sock.family != getattr(socket, 'AF_UNIX', None):
            check(address)
        return connect_ex(sock, address)
    monkeypatch.setattr(socket, 'getaddrinfo', local_resolve)
    monkeypatch.setattr(socket, 'create_connection', local_create)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', local_connect_ex)


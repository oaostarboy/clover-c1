"""Offline regression fixture: fail on physical DNS/socket attempts.

Load explicitly with pytest -p qa.presence.network_guard. HTTP MockTransport and
native PTB synthetic request transports still run their real serializers.
"""
import socket
import pytest


@pytest.fixture(autouse=True)
def presence_offline_network_guard(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('physical networking is forbidden in presence integration regression')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)

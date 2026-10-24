"""Opt-in socket-level egress denial for the owner-attestation review tests.

Load with ``PYTHONPATH=review/owner-attestation python -m pytest -p deny_network``.
This is Python socket enforcement, not a claim of a kernel network namespace.
"""
import socket

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _deny_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError("review tests must not open network connections")
    return _connect(sock, address)


def _deny_connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError("review tests must not open network connections")
    return _connect_ex(sock, address)


socket.socket.connect = _deny_connect
socket.socket.connect_ex = _deny_connect_ex

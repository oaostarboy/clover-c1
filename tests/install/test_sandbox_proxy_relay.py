"""Behavior tests for the dev-sandbox MITM proxy's upstream relay."""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPENSSL = shutil.which("openssl")

pytestmark = pytest.mark.skipif(OPENSSL is None, reason="openssl is required")

RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"


def _load_proxy(monkeypatch, root: Path, certs: Path, real_ca: Path):
    monkeypatch.setattr(sys, "argv", ["proxy.py", str(root), str(certs), str(real_ca)])
    spec = importlib.util.spec_from_file_location(
        "sandbox_proxy_under_test", ROOT / "scripts/sandbox/proxy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_localhost_cert(tmp_path: Path) -> tuple[Path, Path]:
    assert OPENSSL is not None
    cert, key = tmp_path / "up.pem", tmp_path / "up.key"
    subprocess.run(
        [
            OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(key), "-out", str(cert),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )
    return cert, key


def _abrupt_tls_upstream(cert: Path, key: Path):
    """TLS server that sends a full response, then drops TCP with no close_notify."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    def serve():
        raw, _ = listener.accept()
        tls = context.wrap_socket(raw, server_side=True)
        data = b""
        while b"\r\n\r\n" not in data:
            data += tls.recv(4096)
        tls.sendall(RESPONSE)
        # Close the raw descriptor without unwrap(): the TLS layer never
        # sends close_notify.  detach() returns the bare fd number.
        os.close(tls.detach())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread


def test_relay_treats_upstream_close_without_close_notify_as_end_of_response(
    monkeypatch, tmp_path
):
    cert, key = _make_localhost_cert(tmp_path)
    proxy = _load_proxy(monkeypatch, tmp_path / "http", tmp_path / "certs", cert)
    listener, thread = _abrupt_tls_upstream(cert, key)
    client, proxy_side = socket.socketpair()
    try:
        port = listener.getsockname()[1]
        proxy.forward_https(
            proxy_side,
            "localhost",
            port,
            b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n",
        )
        proxy_side.close()
        client.settimeout(5)
        received = b""
        while True:
            part = client.recv(4096)
            if not part:
                break
            received += part
    finally:
        client.close()
        listener.close()
        thread.join(timeout=5)

    assert received == RESPONSE


def test_relay_still_propagates_unrelated_errors(monkeypatch, tmp_path):
    proxy = _load_proxy(monkeypatch, tmp_path, tmp_path, tmp_path)

    class Broken:
        def recv(self, _size):
            raise ssl.SSLError("bad record mac")

    with pytest.raises(ssl.SSLError):
        proxy.relay(Broken(), object())

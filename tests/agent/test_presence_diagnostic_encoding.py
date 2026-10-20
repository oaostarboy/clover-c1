"""Offline IR-DIAG-1 regression: refuse encoded errors before HTTPX decode."""
import json
import socket

import httpx
import pytest

from agent.bounded_completion import CompletionHeld
from tests.agent.test_presence_request_contract_review import invoke, selected


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('diagnostic encoding test attempted network')
    # Reuse the reviewed synchronous stdlib self-pipe capability. Arbitrary
    # application loopback, external connections and DNS remain forbidden.
    from tests.native_boundaries import private_socketpair_boundary
    pair, connect = private_socketpair_boundary(socket.socketpair, socket.socket.connect, denied)
    monkeypatch.setattr(socket, 'socketpair', pair)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


@pytest.mark.parametrize('route', ['connect_loopback', 'connect_external', 'connect_ex', 'factory', 'dns'])
def test_diagnostic_guard_still_refuses_application_network(route):
    with socket.socket() as sock, pytest.raises(pytest.fail.Exception, match='attempted network'):
        if route == 'connect_loopback':
            sock.connect(('127.0.0.1', 9))
        elif route == 'connect_external':
            sock.connect(('192.0.2.1', 443))
        elif route == 'connect_ex':
            sock.connect_ex(('127.0.0.1', 9))
        elif route == 'factory':
            socket.create_connection(('127.0.0.1', 9))
        else:
            socket.getaddrinfo('example.invalid', 443)


@pytest.mark.parametrize('headers', [
    {'content-encoding': 'gzip'},
    {'Content-Encoding': ' GZip \t'},
    {'content-encoding': 'deflate'},
    {'content-encoding': 'br'},
    {'content-encoding': 'zstd'},
    {'content-encoding': 'PRIVATE-unknown'},
    {'content-encoding': ''},
    {'content-encoding': ' \t'},
    {'content-encoding': 'identity, gzip'},
    {'content-encoding': 'gzip, identity'},
    {'content-encoding': 'identity, identity'},
    [('Content-Encoding', 'identity'), ('content-encoding', 'gzip')],
    [('Content-Encoding', 'identity'), ('content-encoding', 'identity')],
])
def test_encoded_error_never_enters_reader_or_decoder(monkeypatch, tmp_path, headers):
    reads, decoders, wires, closed, entry = [], [], [], [], []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            yield b'{"error":{"code":"invalid_value","message":"PRIVATE"}}'

        async def aclose(self):
            closed.append(True)

    original_decoder = httpx.Response._get_content_decoder
    def decoder(self):
        decoders.append(True)
        return original_decoder(self)
    monkeypatch.setattr(httpx.Response, '_get_content_decoder', decoder)
    from agent import bounded_completion
    original_diagnostic = bounded_completion._http_failure_diagnostic
    async def diagnostic(response, deadline):
        # Real HTTPX stream entry must not have eagerly read/decoded the body.
        entry.append((response.is_stream_consumed, hasattr(response, '_content')))
        return await original_diagnostic(response, deadline)
    monkeypatch.setattr(bounded_completion, '_http_failure_diagnostic', diagnostic)

    def respond(request):
        wires.append(request)
        return httpx.Response(400, headers=headers, stream=Body())
    _, config, _, runtime = selected(monkeypatch, tmp_path, respond)
    with pytest.raises(CompletionHeld) as held:
        invoke(config, runtime)
    assert entry == [(False, False)]
    assert reads == decoders == []
    assert len(wires) == 1 and closed == [True]
    assert held.value.reason == 'provider_http_400_usage_unknown'
    # An intentionally omitted encoded body is truncated, not empty or parsed.
    assert held.value.diagnostic == {'http_status': 400, 'body_status': 'truncated',
        'error_type': None, 'error_code': None, 'error_param': None,
        'usage': 'unknown', 'cost': None}
    assert 'PRIVATE' not in json.dumps(held.value.diagnostic)


@pytest.mark.parametrize('headers', [{}, {'Content-Encoding': 'identity'},
                                     {'CONTENT-ENCODING': ' \tIDENTITY\t '}])
@pytest.mark.parametrize('size', [16383, 16384, 16385])
def test_identity_error_remains_bounded_and_redacted(monkeypatch, tmp_path, headers, size):
    core = b'{"error":{"code":"invalid_value","message":"PRIVATE"}}'
    body = core + b' ' * (size - len(core))
    reads, closed, wires = [], [], []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for start in range(0, len(body), 127):
                reads.append(start)
                yield body[start:start + 127]

        async def aclose(self):
            closed.append(True)

    def respond(request):
        wires.append(request)
        return httpx.Response(400, headers=headers, stream=Body())
    _, config, _, runtime = selected(monkeypatch, tmp_path, respond)
    with pytest.raises(CompletionHeld) as held:
        invoke(config, runtime)
    result = held.value.diagnostic
    assert isinstance(result, dict)
    assert result['body_status'] == ('truncated' if size > 16384 else 'json')
    assert result['error_code'] == (None if size > 16384 else 'invalid_value')
    assert result['usage'] == 'unknown' and result['cost'] is None
    assert 'PRIVATE' not in json.dumps(result)
    assert reads and len(wires) == 1 and closed == [True]

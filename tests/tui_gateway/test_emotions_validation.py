"""Synthetic command validation, using actual authenticated RPC dispatch."""
import pytest
from tests.gateway.test_emotions_independent_review import host  # noqa: F401


def dispatch_reply(server, transport, request):
    import threading
    completed = threading.Event()
    original = transport.write
    def capture(frame):
        result = original(frame)
        if frame.get('id') == request['id']:
            completed.set()
        return result
    transport.write = capture
    reply = server.dispatch(request, transport=transport)
    if reply is None:
        assert completed.wait(5), transport.frames
        reply = next(frame for frame in transport.frames if frame.get('id') == request['id'])
    return reply


@pytest.mark.parametrize('method', ['command.dispatch', 'slash.exec'])
@pytest.mark.parametrize('sid', [None, [], {}, 17, '', 'runtime\u200b', 'runtime\n', 'é'*600, '\ud800'])
def test_raw_session_shape_returns_jsonrpc_error(host, monkeypatch, method, sid):
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    agent, transport, _, session, send = desktop_host(host, monkeypatch)
    try:
        params = {'session_id':sid, 'name':'emotions', 'command':'/emotions'}
        reply = dispatch_reply(server, transport, {'jsonrpc':'2.0','id':'bad','method':method,'params':params})
        assert 'error' in reply, reply
        assert reply['error']['code'] in {-32602, 4001}
        assert 'Provider route:' not in str(reply)
    finally:
        agent.close()


@pytest.mark.parametrize('method', ['command.dispatch', 'slash.exec'])
@pytest.mark.parametrize('argument', ['set happy', 'on', 'off'])
def test_actual_desktop_arguments_explicitly_rejected(host, monkeypatch, method, argument):
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    agent, transport, sid, session, send = desktop_host(host, monkeypatch)
    try:
        params = {'session_id':sid, 'name':'emotions', 'arg':argument, 'command':'/emotions '+argument}
        reply = dispatch_reply(server, transport, {'jsonrpc':'2.0','id':'arg','method':method,'params':params})
        assert 'takes no arguments' in str(reply), reply
        assert not host.requests and not session['history']
    finally:
        agent.close()


def test_unknown_literal_session_is_not_repaired_or_normalized(host, monkeypatch):
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    agent, transport, sid, session, send = desktop_host(host, monkeypatch)
    try:
        send('Synthetic fixture: I am frustrated.')
        before = set(server._sessions)
        for candidate in [sid+' ', ' '+sid, sid+'\u200b', sid.upper()]:
            reply = server.dispatch({'jsonrpc':'2.0','id':'unknown','method':'command.dispatch',
                'params':{'session_id':candidate, 'name':'emotions'}}, transport=transport)
            assert 'confidence=' not in str(reply)
        assert set(server._sessions) == before
    finally:
        agent.close()

"""Native Telegram update identity, not a claim of a remote rewrite exploit."""
import asyncio
import pytest
from tests.gateway.test_emotions_independent_review import host, seed  # noqa: F401


def receive(h, mp, update_id, message_id):
    events = []
    with mp.context() as patch:
        patch.setattr(h.adapter, '_enqueue_text_event', events.append)
        update = h.update('/emotions', message_id=message_id)
        update.update_id = update_id
        asyncio.run(h.adapter._handle_text_message(update, object()))
    assert len(events) == 1
    assert events[0].platform_update_id == update_id
    return events[0]


def invoke(h, event):
    return asyncio.run(h.runner._handle_emotions_command(event))


def test_native_update_rebuild_rejected_across_message_and_audience(host, monkeypatch):
    seed(host)
    first = receive(host, monkeypatch, 501, 201)
    rebuilt = receive(host, monkeypatch, 501, 202)
    distinct = receive(host, monkeypatch, 502, 203)
    assert 'confidence=0.90' in invoke(host, first)
    assert 'Provider route:' not in invoke(host, rebuilt)
    assert 'confidence=0.90' in invoke(host, distinct)
    assert 'Provider route:' not in invoke(host, distinct)  # one use


@pytest.mark.parametrize('native_id', [None, True, False, -1, 2**63, '501', ' 501', '５01', [], {}])
def test_missing_or_malformed_native_update_is_not_command_authority(host, monkeypatch, native_id):
    seed(host)
    event = receive(host, monkeypatch, native_id, 211)
    assert 'Provider route:' not in invoke(host, event)


def test_native_zero_update_is_literal_not_missing(host, monkeypatch):
    seed(host)
    assert 'confidence=0.90' in invoke(host, receive(host, monkeypatch, 0, 212))


def test_update_guard_is_bounded_and_ages_out(host, monkeypatch):
    from agent import workstream_origin as origin
    monkeypatch.setattr(origin, '_COMMAND_UPDATES', {(i,): float('inf') for i in range(4096)}, raising=False)
    assert 'Provider route:' not in invoke(host, receive(host, monkeypatch, 601, 221))
    monkeypatch.setattr(origin, '_COMMAND_UPDATES', {(i,): -1 for i in range(4096)})
    assert 'Provider route:' in invoke(host, receive(host, monkeypatch, 602, 222))
    assert len(origin._COMMAND_UPDATES) == 1

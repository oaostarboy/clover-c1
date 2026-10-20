"""RG-001: the real after-delivery owner must finish before asyncio.run closes."""
import asyncio

import pytest
from agent.presence_runtime import PresenceTurn
from tests.gateway import test_presence_integration as original
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


@pytest.mark.parametrize('case', ['revoke', 'source_corrected', 'unknown'])
def test_inbound_fixture_waits_for_delayed_actual_after_reply_owner(monkeypatch, tmp_path, case):
    events = []
    publish = PresenceTurn.publish

    async def delayed(host, *args, **kwargs):
        events.append('started')
        try:
            # Scheduler delay only. Keep every real provider, policy, receipt,
            # source, expiry, ACK and unknown-outcome assertion unchanged.
            await asyncio.sleep(.35)
            result = await publish(host, *args, **kwargs)
            events.append('completed')
            return result
        except asyncio.CancelledError:
            events.append('cancelled')
            raise

    monkeypatch.setattr(PresenceTurn, 'publish', delayed)
    if case == 'revoke':
        original.test_revocation_after_http_prevents_commit_and_publication(monkeypatch, tmp_path, 'revoke')
    elif case == 'source_corrected':
        original.test_source_correction_inside_proposal_transaction_rejects_commit(monkeypatch, tmp_path)
    else:
        original.test_unknown_native_send_is_held_across_next_event(monkeypatch, tmp_path)
    assert 'completed' in events
    assert 'cancelled' not in events, 'fixture closed its event loop with real owner work pending'

"""Builder regression probes: real compose/owner, synthetic provider and transport."""
import pytest
from cron.presence import ThoughtStore
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


@pytest.mark.parametrize('race', ['stop', 'new_input'])
def test_posture_revoked_after_compose_before_provider(monkeypatch, tmp_path, race):
    from run_agent import AIAgent
    real = AIAgent._build_api_kwargs
    revoked = []
    def build(agent, messages, *args, **kwargs):
        result = real(agent, messages, *args, **kwargs)
        if 'Tentative interaction posture' in str(result) and not revoked:
            store = ThoughtStore()
            store.stop() if race == 'stop' else store.advance_generation()
            revoked.append(race)
        return result
    monkeypatch.setattr(AIAgent, '_build_api_kwargs', build)
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('The previous attempt failed and I am frustrated. Please be brief.', message_id=100)
        assert revoked == [race]
        assert len(requests) == 1 and bot.send_message.await_count == 0
        assert h.results[-1]['final_response'] == 'ack'
        assert 'Tentative interaction posture' not in str(h.scripted.requests[-1]['messages'])
        assert 'Tentative interaction posture' not in str(h.user_rows()[-1]['api_content'])
    finally:
        h.close()

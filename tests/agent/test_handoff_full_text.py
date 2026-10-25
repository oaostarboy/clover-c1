"""Full user-facing handoffs: real registered dispatch -> parent -> Telegram.

Only model and Bot API boundaries are scripted. No network or live state.
"""
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai.types.chat import ChatCompletion

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from gateway.config import PlatformConfig
from gateway.platforms.base import utf16_len
from plugins.platforms.telegram.adapter import TelegramAdapter
from run_agent import AIAgent
from tests.agent.test_delegation_router import aux, verdict_json  # noqa: F401
from tests.agent.test_foreground_time_limit_loop import make_agent, text, tool
from tests.agent.test_model_dispatch_opener import _unified  # noqa: F401
from tests.agent.test_router_backstop import _clean_registry, dispatches  # noqa: F401
from tests.agent.test_router_e2e import _unbind_session_context, children  # noqa: F401
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401

pytestmark = [pytest.mark.real_delegation_router, pytest.mark.unified_card]

# Newlines, repeated spaces, Markdown, non-BMP emoji and combining characters.
# No line lies on a chunk boundary: the transport may add numbering but must
# preserve the exact formatted body, not a whitespace-normalized approximation.
LONG_SAY = ('BEGIN handoff **answer** 🙂\n\n'
            + ''.join(f'row{i:04d} 雪🙂e\u0301  checking the full answer\n' for i in range(160))
            + '\nMID handoff `detail`\n'
            + ''.join(f'tail{i:04d} more complete details here\n' for i in range(100))
            + '\nEND handoff complete')
HANDOFF = {'work': 'checking the handoff', 'outcome': 'complete answer'}


@pytest.fixture
def provider(monkeypatch):
    """In-process provider; even loopback HTTP is forbidden."""
    p = SimpleNamespace(queue=[], captured=[], base_url='http://127.0.0.1:1/v1')
    client = MagicMock()

    def create(**kwargs):
        p.captured.append(kwargs)
        assert not kwargs.get('stream'), 'fixture expects non-streaming model calls'
        payload = p.queue.pop(0) if p.queue else text('DONE')
        return ChatCompletion.model_validate(dict(id='synthetic', created=0,
                                                  model='scripted', object='chat.completion',
                                                  **payload))

    client.chat.completions.create.side_effect = create
    monkeypatch.setattr(AIAgent, '_create_openai_client', lambda *_a, **_kw: client)
    for target in ('agent.model_metadata.get_model_context_length',
                   'agent.context_compressor.get_model_context_length'):
        monkeypatch.setattr(target, lambda *_a, **_kw: 100_000)
    for target in ('agent.model_metadata.query_ollama_num_ctx',
                   'agent.agent_init.query_ollama_num_ctx'):
        monkeypatch.setattr(target, lambda *_a, **_kw: None)
    return p


def run_dispatch(provider, aux, *, say=LONG_SAY, platform='telegram'):
    aux.replies[:] = [verdict_json('direct')]
    handoff = dict(HANDOFF)
    if say is not None:
        handoff['say'] = say
    provider.queue[:] = [tool('delegate_task', {
        'tasks': [{'goal': 'Inspect the handoff text', 'context': 'synthetic read-only task'}],
        'handoff': handoff,
    }, 'full-text-call'), text('The helper is checking it now.')]
    agent = make_agent(provider, platform=platform)
    result = agent.run_conversation('Check the handoff text.', conversation_history=[],
                                    task_id='full-text-test')
    return agent, result


@pytest.mark.asyncio
async def test_registered_dispatch_preserves_full_say_in_parent_reply(
        provider, aux, children, dispatches, network_guard):
    agent, result = run_dispatch(provider, aux)
    reply = result['final_response']
    assert result['turn_exit_reason'] == 'delegation_handoff'
    assert len(fl.own_dispatch_receipts(agent)) == 1
    assert reply == LONG_SAY
    assert fl.own_dispatch_receipts(agent)[0].say == LONG_SAY
    assert len(provider.captured) == 1, 'do not ask another model to rewrite the answer'
    card = ac.card_for(fl.own_dispatch_receipts(agent))
    assert card is not None and card.say_text() == LONG_SAY


@pytest.mark.asyncio
async def test_telegram_transport_preserves_every_handoff_character(
        provider, aux, children, dispatches, network_guard):
    # Kept as a strict failing probe: the existing generic splitter removes
    # non-code whitespace at boundaries. Do not weaken equality to hide it.
    agent, result = run_dispatch(provider, aux)
    reply = result['final_response']
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic',
                                             extra={'rich_messages': False}))
    wires = []

    async def send_message(**kwargs):
        wires.append(kwargs['text'])
        return SimpleNamespace(message_id=len(wires))

    adapter._bot = SimpleNamespace(send_message=send_message)
    sent = await adapter.send('12345', reply, metadata={'notify': True})
    assert sent.success
    # Sending the real parent reply also binds/sends its independent activity
    # card. It is not part of the narrative payload.
    wires = [chunk for chunk in wires if not chunk.startswith('**>')]
    assert len(wires) > 1, 'exercise real transport splitting'
    assert all(utf16_len(chunk) <= adapter.MAX_MESSAGE_LENGTH for chunk in wires)
    # Remove ONLY the transport-added numbered suffix; no stripping source
    # whitespace or Markdown escapes to make a lossy reconstruction pass.
    payloads = [re.sub(r' \\\(\d+/\d+\\\)$', '', chunk) for chunk in wires]
    assert ''.join(payloads) == adapter.format_message(LONG_SAY)
    assert sum('BEGIN handoff' in chunk for chunk in wires) == 1
    assert sum('MID handoff' in chunk for chunk in wires) == 1
    assert sum('END handoff' in chunk for chunk in wires) == 1


def test_public_handoff_schema_accepts_full_narrative():
    from jsonschema import validate
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA

    validate({'handoff': dict(HANDOFF, say=LONG_SAY)},
             DELEGATE_TASK_SCHEMA['parameters'])


def test_generated_handoff_opener_keeps_complete_text():
    from agent.handoff_notes import _clean_opener

    assert _clean_opener(LONG_SAY) == LONG_SAY

"""Live Discord SDK input owner -> real authorized gateway/loop/SQLite consumer.

No Discord connection or person send: SDK frames and provider edges are local.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock

import discord
import pytest

from agent import workstreams
from cron import notepad
from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway.test_trusted_human_origin_ingress import Harness, network_guard  # noqa: F401
from tests.cli.test_workstream_composer_ingress import observe_edges, save_edges


@pytest.fixture
def channel(monkeypatch, tmp_path, request):
    h = Harness(monkeypatch, tmp_path, proactive={'enabled': True, 'recall': {'enabled': True}})
    observe_edges(h, monkeypatch, request)
    monkeypatch.chdir(h.project)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '12345')
    monkeypatch.delenv('GATEWAY_ALLOW_ALL_USERS', raising=False)
    h.runner._is_user_authorized = GatewayRunner._is_user_authorized.__get__(h.runner)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', extra={}))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=99999))
    adapter._allowed_user_ids = {'12345'}
    adapter._ready_event.set()
    adapter._text_batch_delay_seconds = .01
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id='local-out'))
    adapter.set_message_handler(h.runner._handle_message)
    h.adapter = adapter
    h.runner.adapters = {Platform.DISCORD: adapter}
    from run_agent import AIAgent
    h.observed = []
    real_run = AIAgent.run_conversation
    def observe(agent, *a, **kw):
        ingress = kw.get('trusted_human_ingress')
        h.observed.append((getattr(ingress, 'scope', None), workstreams.scope_for_agent(agent)))
        return real_run(agent, *a, **kw)
    monkeypatch.setattr(AIAgent, 'run_conversation', observe)
    yield h
    save_edges(h, request)
    h.close()


def message(text='Correction: use discord-X not discord-Y', *, mid=100, actor=12345, bot=False):
    chan = MagicMock(spec=discord.DMChannel)
    chan.id = 54321
    msg = SimpleNamespace(id=mid, content=text, channel=chan, guild=None,
        author=SimpleNamespace(id=actor, bot=bot, name='fixture', display_name='fixture'),
        type=discord.MessageType.default, mentions=[], attachments=[], reference=None,
        webhook_id=None, message_snapshots=[], created_at=datetime.now(timezone.utc))
    return msg


def dispatch(h, msg, *, recovery=False):
    async def run():
        before = h.scripted.calls
        if recovery:
            await h.adapter._handle_message(msg, recovered=True)
        else:
            await h.adapter._dispatch_discord_message(msg)
        # Adapter rejection is synchronous; admitted turns settle through the real runner.
        if h.scripted.calls > before or h.adapter._pending_text_batch_tasks:
            await h.settle(calls=before + 1)
    asyncio.run(run())


def scope(h):
    return workstreams.scope_for_surface(platform='discord', chat_type='dm',
        chat_id='54321', user_id='12345', project=str(h.project))


def test_live_authorized_discord_dm_correction_and_next_turn_recall(channel):
    dispatch(channel, message())
    records = notepad.list_workstreams(scope(channel))
    assert len(records) == 1 and records[0]['kind'] == 'correction', '\n'.join(str(v) for pair in channel.observed for v in pair)
    assert scope(channel).audience == 'private:12345' and scope(channel).root == '12345'
    with channel.db._read_ctx() as db:
        row = db.execute('SELECT message_row_id,consumed_by FROM human_origin_receipts').fetchone()
        assert row[1] == 'workstream_human_hooks'
    dispatch(channel, message('Which discord-X path is configured?', mid=101))
    assert records[0]['workstream_id'] in str(channel.scripted.requests[-1])
    assert channel.scripted.calls == 2


@pytest.mark.parametrize('kind', ['bot', 'webhook', 'snapshot', 'recovery', 'noauth'])
def test_nonlive_nonhuman_or_unauthorized_discord_input_cannot_write(channel, kind):
    msg = message(bot=kind == 'bot', actor=77777 if kind == 'noauth' else 12345)
    if kind == 'webhook':
        msg.webhook_id = 123
    if kind == 'snapshot':
        msg.message_snapshots = [SimpleNamespace(content='Correction: imported text', attachments=[])]
    dispatch(channel, msg, recovery=kind == 'recovery')
    assert notepad.list_workstreams(scope(channel)) == []
    with channel.db._read_ctx() as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='human_origin_receipts'").fetchone()[0] == 0
    if kind == 'noauth':
        assert channel.scripted.calls == 0


@pytest.mark.parametrize('change', ['actor', 'audience', 'stop'])
def test_discord_live_turn_scope_cannot_change_after_persistence(channel, monkeypatch, change):
    from run_agent import AIAgent
    real = AIAgent._persist_session
    changed_scopes = []
    def persist_then_change(agent, *args, **kwargs):
        out = real(agent, *args, **kwargs)
        if not changed_scopes:
            if change == 'stop':
                agent.interrupt(hard_cancel=True)
                agent.clear_interrupt()
            else:
                agent._user_id = 'foreign-actor'
                if change == 'audience':
                    agent._chat_type, agent._chat_id = 'group', 'foreign-group'
            changed_scopes.append(workstreams.scope_for_agent(agent))
        return out
    monkeypatch.setattr(AIAgent, '_persist_session', persist_then_change)
    dispatch(channel, message())
    assert changed_scopes and notepad.list_workstreams(scope(channel)) == []
    assert notepad.list_workstreams(changed_scopes[0]) == []

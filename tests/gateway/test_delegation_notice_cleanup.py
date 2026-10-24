"""Real progress producer, Telegram transport, and post-delivery cleanup.

The fake Bot API records server messages before acknowledging sends. No live
Telegram connection is used. Delayed receipts reproduce the dispatch notice
that otherwise survives turn teardown with no tracked message ID.
"""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_run_cleanup_progress import (
    _fire_post_delivery_cb, _install_fakes, _make_runner,
)

CHAT = '-1001'
KEY = 'agent:main:telegram:group:-1001'
NOTICE = 'Delegating 2 subagents'


class RecordingBot:
    def __init__(self, *, delay_notice=False, delay_edit=False, reject_edits=False):
        self.messages = {}
        self.sends = []
        self.edits = []
        self.deletes = []
        self.notice_started = threading.Event()
        self.edit_started = threading.Event()
        self.release_notice = asyncio.Event()
        self.release_edit = asyncio.Event()
        self.delay_notice = delay_notice
        self.delay_edit = delay_edit
        self.reject_edits = reject_edits

    async def send_message(self, **kwargs):
        # Telegram IDs are chat-local: different chats may share the same ID.
        mid = sum(r['chat_id'] == kwargs['chat_id'] for r in self.sends) + 100
        record = dict(kwargs, message_id=mid)
        self.sends.append(record)
        self.messages[(kwargs['chat_id'], mid)] = kwargs['text']
        if NOTICE in kwargs['text']:
            self.notice_started.set()
            if self.delay_notice:
                await self.release_notice.wait()
        return SimpleNamespace(message_id=mid)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        if self.reject_edits:
            raise BadRequest('message to edit not found')
        if self.delay_edit and NOTICE in kwargs['text']:
            self.edit_started.set()
            await self.release_edit.wait()
        self.messages[(kwargs['chat_id'], kwargs['message_id'])] = kwargs['text']
        return SimpleNamespace(message_id=kwargs['message_id'])

    async def delete_message(self, **kwargs):
        self.deletes.append(kwargs)
        self.messages.pop((kwargs['chat_id'], kwargs['message_id']), None)
        return True

    async def send_chat_action(self, **kwargs):
        pass


async def _settle():
    for _ in range(30):
        await asyncio.sleep(0.01)


def _setup(monkeypatch, tmp_path, bot, *, cleanup=True, outcome='success', repeated=False,
           grouping='accumulate', edit_race=False, queued=False):
    class DispatchAgent:
        def __init__(self, **kwargs):
            self.tools = []
            self.tool_progress_callback = None

        def run_conversation(self, message, **kwargs):
            if message == 'followup':
                return {'final_response': 'Follow-up answer.', 'messages': [], 'api_calls': 1}
            assert self.tool_progress_callback is not None
            for _ in range(2 if repeated else 1):
                self.tool_progress_callback('tool.started', 'delegate_task', '', {
                    'tasks': [{'goal': 'first'}, {'goal': 'second'}],
                })
            assert bot.notice_started.wait(3), 'actual progress send never started'
            if edit_race:
                # Let the real progress edit throttle expire, then force an
                # edit containing the delegation line to race final delivery.
                time.sleep(1.6)
                self.tool_progress_callback('tool.started', 'terminal', 'pwd', {})
                assert bot.edit_started.wait(3), 'actual edit never started'
            if outcome == 'failed':
                return {'final_response': '', 'messages': [], 'api_calls': 1,
                        'failed': True, 'error': 'spawn failed'}
            if outcome == 'cancelled':
                return {'final_response': 'Dispatch was stopped.', 'messages': [],
                        'api_calls': 1, 'interrupted': True}
            return {'final_response': 'The two subagents are working.', 'messages': [], 'api_calls': 1}

    # Register the real delegation tool's progress emoji, not a test label.
    import tools.delegate_tool  # noqa: F401

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='test-token'))
    adapter._bot = bot
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, DispatchAgent, cleanup_on=cleanup)
    monkeypatch.setattr(gateway_run, '_clover_home', tmp_path)
    monkeypatch.setattr(gateway_run, '_load_gateway_config', lambda: {
        'display': {'tool_progress': 'all', 'tool_progress_grouping': grouping,
                    'delegation_activity': 'off',
                    'platforms': {'telegram': {'cleanup_progress': cleanup}}},
    })
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT)
    if queued:
        adapter._pending_messages[KEY] = MessageEvent(
            text='followup', message_type=MessageType.TEXT, source=source,
        )
    return runner, adapter, source


async def _run(runner, source, *, generation=None, key=KEY):
    return await runner._run_agent(
        message='dispatch', context_prompt='', history=[], source=source,
        session_id='notice-race', session_key=key, run_generation=generation,
    )


async def _deliver(adapter, result, *, generation=None, key=KEY, chat=CHAT):
    reply = await adapter.send(chat, result['final_response'])
    cb = adapter.pop_post_delivery_callback(key, generation=generation)
    if cb:
        await _fire_post_delivery_cb(cb)
    await _settle()
    return reply


def _assert_no_notice(bot):
    assert not any(NOTICE in text for text in bot.messages.values()), (
        'the exact dispatch tool-progress notice survived final delivery', bot.messages,
    )


def _assert_summary(bot):
    assert any('tool call' in text for text in bot.messages.values()), 'keep the collapsed turn summary'
    assert all(r.get('parse_mode') == 'MarkdownV2' for r in bot.edits)
    # Telegram expandable quotes use a required empty-bold **> prefix.
    assert not any('**' in text.replace('**>', '') for text in bot.messages.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('delay_notice', [False, True])
@pytest.mark.parametrize('grouping', ['accumulate', 'separate'])
async def test_dispatch_notice_disappears_only_after_delivery(monkeypatch, tmp_path, delay_notice, grouping):
    bot = RecordingBot(delay_notice=delay_notice)
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot, grouping=grouping)
    # A worker card is persistent status, not a temporary progress receipt.
    worker = await adapter.send(CHAT, '> **Worker status**\n> *tap to watch 🍀*\n> ⠀\n> Running.')
    result = await _run(runner, source)
    notices = [r for r in bot.sends if NOTICE in r['text']]
    assert len(notices) == 1
    notice = notices[0]
    assert (notice['chat_id'], notice['message_id']) in bot.messages
    assert not bot.deletes, 'progress must remain visible before final delivery'
    reply = await _deliver(adapter, result)
    assert not delay_notice or any(NOTICE in text for text in bot.messages.values())
    bot.release_notice.set()
    await _settle()
    _assert_no_notice(bot)
    assert worker.message_id and reply.message_id
    assert (-1001, int(worker.message_id)) in bot.messages
    assert (-1001, int(reply.message_id)) in bot.messages
    _assert_summary(bot)


@pytest.mark.asyncio
async def test_pending_edit_cannot_restore_dispatch_notice_after_collapse(monkeypatch, tmp_path):
    bot = RecordingBot(delay_edit=True)
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot, edit_race=True)
    # Keep the cancellation-handler drain bounded even though its second edit
    # is also held in flight. The actual network requests must survive it.
    import gateway.run as run
    monkeypatch.setattr(run, '_TEARDOWN_TASK_GRACE_SECONDS', 0.02)
    result = await _run(runner, source)
    reply = await _deliver(adapter, result)
    bot.release_edit.set()
    await _settle()
    _assert_no_notice(bot)
    _assert_summary(bot)
    assert reply.message_id and (-1001, int(reply.message_id)) in bot.messages
    assert any('pwd' in r['text'] for r in bot.edits), 'ordinary tools stay visible'


@pytest.mark.asyncio
@pytest.mark.parametrize('delay_notice', [False, True])
async def test_repeated_dispatches_still_collapse_into_one_summary(monkeypatch, tmp_path, delay_notice):
    bot = RecordingBot(delay_notice=delay_notice)
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot, repeated=True)
    result = await _run(runner, source)
    await _deliver(adapter, result)
    bot.release_notice.set()
    await _settle()
    _assert_no_notice(bot)
    assert sum('tool call' in text for text in bot.messages.values()) == 1
    assert any('2 tool calls' in text for text in bot.messages.values())


@pytest.mark.asyncio
async def test_failed_spawn_retains_breadcrumb_without_acceptance_claim(monkeypatch, tmp_path):
    bot = RecordingBot(delay_notice=True)
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot, outcome='failed')
    result = await _run(runner, source)
    assert result['failed']
    await _deliver(adapter, result)
    bot.release_notice.set()
    await _settle()
    assert any(NOTICE in text for text in bot.messages.values())
    assert not bot.deletes
    assert not any('subagents are working' in text for text in bot.messages.values())


@pytest.mark.asyncio
async def test_cancelled_dispatch_does_not_claim_accepted_work(monkeypatch, tmp_path):
    bot = RecordingBot(delay_notice=True)
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot, outcome='cancelled')
    result = await _run(runner, source)
    assert result['interrupted']
    await _deliver(adapter, result)
    bot.release_notice.set()
    await _settle()
    assert not any('subagents are working' in text for text in bot.messages.values())
    assert any('stopped' in text for text in bot.messages.values())


@pytest.mark.asyncio
async def test_cleanup_optout_keeps_the_actual_progress_notice(monkeypatch, tmp_path):
    bot = RecordingBot()
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot, cleanup=False)
    result = await _run(runner, source)
    await _deliver(adapter, result)
    assert any(NOTICE in text for text in bot.messages.values())
    assert not bot.deletes
    assert not any('tool call' in r['text'] for r in bot.edits)


@pytest.mark.asyncio
async def test_failed_card_edit_deletes_only_temporary_notice(monkeypatch, tmp_path):
    from gateway import progress_cleanup
    monkeypatch.setattr(progress_cleanup, '_RETRY_DELAYS', ())
    bot = RecordingBot(delay_notice=True, reject_edits=True)
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot)
    worker = await adapter.send(CHAT, 'Persistent worker status')
    result = await _run(runner, source)
    reply = await _deliver(adapter, result)
    bot.release_notice.set()
    await _settle()
    _assert_no_notice(bot)
    assert len(bot.deletes) == 1
    assert worker.message_id and reply.message_id
    assert (-1001, int(worker.message_id)) in bot.messages
    assert (-1001, int(reply.message_id)) in bot.messages


@pytest.mark.asyncio
async def test_queued_followup_late_receipt_keeps_both_replies_and_card(monkeypatch, tmp_path):
    bot = RecordingBot(delay_notice=True)
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot, queued=True)
    worker = await adapter.send(CHAT, 'Persistent worker status')
    result = await _run(runner, source)
    assert result['final_response'] == 'Follow-up answer.'
    await _deliver(adapter, result)
    bot.release_notice.set()
    await _settle()
    _assert_no_notice(bot)
    _assert_summary(bot)
    assert worker.message_id and (-1001, int(worker.message_id)) in bot.messages
    assert sum('subagents are working' in text for text in bot.messages.values()) == 1
    assert sum('Follow' in text for text in bot.messages.values()) == 1


@pytest.mark.asyncio
async def test_foreign_generation_cannot_fire_cleanup(monkeypatch, tmp_path):
    bot = RecordingBot(delay_notice=True)
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot)
    runner._session_run_generation[KEY] = 7
    result = await _run(runner, source, generation=7)
    assert adapter.pop_post_delivery_callback(KEY, generation=8) is None
    bot.release_notice.set()
    await _settle()
    assert any(NOTICE in text for text in bot.messages.values()), 'foreign pop must not clean this turn'
    foreign = []
    adapter.register_post_delivery_callback('other-chat', lambda: foreign.append('fired'), generation=8)
    await _deliver(adapter, result, generation=7)
    _assert_no_notice(bot)
    assert foreign == []
    assert adapter.pop_post_delivery_callback(KEY, generation=7) is None, 'exactly once'
    assert callable(adapter.pop_post_delivery_callback('other-chat', generation=8))


@pytest.mark.asyncio
@pytest.mark.parametrize('same_chat', [False, True])
async def test_old_receipt_arrives_after_new_turn_cleanup(monkeypatch, tmp_path, same_chat):
    bot = RecordingBot(delay_notice=True)
    runner, adapter, source = _setup(monkeypatch, tmp_path, bot)
    runner._session_run_generation[KEY] = 7
    first = await _run(runner, source, generation=7)
    first_reply = await _deliver(adapter, first, generation=7)
    # The older cleanup task owns only its original context, even if another
    # generation starts, or another chat uses the very same Telegram ID.
    next_chat = CHAT if same_chat else '-2002'
    next_key = KEY if same_chat else 'agent:main:telegram:group:-2002'
    runner._session_run_generation[next_key] = 8
    bot.delay_notice = False
    bot.notice_started.clear()
    newer_source = SessionSource(platform=Platform.TELEGRAM, chat_id=next_chat)
    worker = await adapter.send(next_chat, 'Persistent worker status')
    second = await _run(runner, newer_source, generation=8, key=next_key)
    second_reply = await _deliver(adapter, second, generation=8, key=next_key, chat=next_chat)
    bot.release_notice.set()
    await _settle()
    _assert_no_notice(bot)
    assert sum('tool call' in text for text in bot.messages.values()) == 2
    assert worker.message_id and first_reply.message_id and second_reply.message_id
    assert (-1001, int(first_reply.message_id)) in bot.messages
    assert (int(next_chat), int(worker.message_id)) in bot.messages
    assert (int(next_chat), int(second_reply.message_id)) in bot.messages

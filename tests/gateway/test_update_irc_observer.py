"""Portable replay of the native proof's IRC observer, not native acceptance.

The observer class is compiled unchanged from its actual fixture source, without
importing or invoking the native driver, installer, updater or provider.
"""
import ast
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / '.github/owner-worker-proof/windows_update.py'


def observer_type():
    tree = ast.parse(DRIVER.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LocalIRC')
    namespace: dict[str, Any] = dict(threading=threading, time=time)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(DRIVER), 'exec'), namespace)
    return namespace['LocalIRC']


@pytest.fixture
def cp1252_observer(tmp_path, monkeypatch):
    """Reproduce Windows' file default, not stdout's unrelated encoding.

    Python -I ignores PYTHONUTF8/PYTHONIOENCODING inherited from the parent.
    Force the observed file-open default only; explicit encodings still win.
    """
    path = tmp_path / 'irc.log'
    real_open = Path.open

    def windows_open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        if self == path and 'b' not in mode and encoding is None:
            encoding = 'cp1252'
        return real_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, 'open', windows_open)
    cls = observer_type()
    observer = object.__new__(cls)  # Do not bind the native fixture's fixed port.
    observer.evidence = tmp_path
    observer.lock = threading.Lock()
    return observer, path


@pytest.mark.parametrize('text', ['☘ Updating Clover…', '✅ Clover update finished.',
                                 '✅ Clover update finished successfully.'])
def test_observer_records_actual_unicode_messages_as_utf8(cp1252_observer, text):
    observer, path = cp1252_observer
    observer.log('<< PRIVMSG ant :' + text)
    assert ('<< PRIVMSG ant :' + text + '\n').encode('utf-8') in path.read_bytes()


def final_notice_predicate(path, offset):
    """Execute the driver's original positive current-window predicate verbatim."""
    tree = ast.parse(DRIVER.read_text(encoding='utf-8'))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == 'final_notice')
    namespace: dict[str, Any] = dict(evidence=path.parent, update_offsets={str(path): offset})
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(DRIVER), 'exec'), namespace)
    return namespace['final_notice']


@asynccontextmanager
async def observed_adapter(observer):
    """Real async IRC writes and unchanged threaded receiver; no remote service."""
    from gateway.config import PlatformConfig
    from tests.gateway._plugin_adapter_loader import load_plugin_adapter

    adapter = load_plugin_adapter('irc').IRCAdapter(PlatformConfig(enabled=True))
    server, client = socket.socketpair()
    _, writer = await asyncio.open_connection(sock=client)
    adapter._writer = writer
    errors = []
    state = dict(sock=server, nick='cloverbot', ready=True)

    def receive():
        try:
            observer.handle(state)
        except Exception as exc:
            errors.append(exc)  # Make the original thread crash an assertion, not a warning.

    thread = threading.Thread(target=receive)
    thread.start()
    try:
        yield adapter, errors
    finally:
        writer.close()
        await writer.wait_closed()
        await asyncio.to_thread(thread.join, 2)
        server.close()
        assert not thread.is_alive()


def runner():
    from gateway.run import GatewayRunner
    result = object.__new__(GatewayRunner)
    result.adapters = {}
    result._voice_mode = {}
    result._update_prompt_pending = {}
    return result


CALLERS = [
    ('3b54f413376eb99f8b83bfd4a0a340d01825014d',
     'f49296084d8e5914998f18726d24905c566f13fb'),
    ('f49296084d8e5914998f18726d24905c566f13fb',
     'c670b4f6717950d11b167e9946a86f3f7c428e80'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('old,target', CALLERS, ids=['released159', 'sameversion-candidate'])
@pytest.mark.parametrize('boundary', ['before-exit', 'adapter-offline', 'claimed-recovery'])
async def test_real_success_notice_survives_observer_and_restart_boundaries(
    tmp_path, monkeypatch, cp1252_observer, old, target, boundary,
):
    import gateway.run as gateway_run
    from gateway.config import Platform

    observer, path = cp1252_observer
    # A historical success must not satisfy the current-run final-notice gate.
    path.write_bytes(b'1 << PRIVMSG ant :historical Clover update finished.\n')
    offset = path.stat().st_size
    final_notice = final_notice_predicate(path, offset)
    assert final_notice() is None
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(gateway_run, '_clover_home', home)
    pending = home / '.update_pending.json'
    claimed = home / '.update_pending.claimed.json'
    output = home / '.update_output.txt'
    exit_code = home / '.update_exit_code'
    marker = dict(platform='irc', chat_id='ant', chat_type='dm',
                  session_key='irc:ant', message_id='current-update',
                  updater_pid=os.getpid(), caller_sha=old, target_sha=target)
    pending.write_text(json.dumps(marker), encoding='utf-8')
    # Include source identity in the real rendered output, not merely the log label.
    output.write_text('✓ Update complete! (v1.5.9) [' + target + ']', encoding='utf-8')
    first = runner()
    async with observed_adapter(observer) as (adapter, errors):
        if boundary == 'before-exit':
            first.adapters = {Platform('irc'): adapter}
            assert await first._send_update_notification() is False
        elif boundary == 'adapter-offline':
            exit_code.write_text('0', encoding='utf-8')
            assert await first._send_update_notification() is False
        else:
            pending.replace(claimed)  # A notifier died after claiming, before sending.
        active = pending if pending.exists() else claimed
        assert json.loads(active.read_text(encoding='utf-8')) == marker
        assert output.exists()
        assert final_notice() is None

        # New runner reads durable state; no updater/tool/model action is replayed.
        successor = runner()
        successor.adapters = {Platform('irc'): adapter}
        exit_code.write_text('0', encoding='utf-8')
        assert await successor._send_update_notification() is True
        assert await successor._send_update_notification() is False
    assert errors == [], repr(errors)
    notices = final_notice()
    assert notices is not None and len(notices) == 1
    current = path.read_bytes()[offset:].decode('utf-8')
    assert '<< PRIVMSG ant :✅ Clover update finished.' in current
    assert target in current
    assert not any(p.exists() for p in (pending, claimed, output, exit_code))


@pytest.mark.asyncio
async def test_real_async_message_queue_records_unicode_reply_once(
    monkeypatch, cp1252_observer,
):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource

    observer, path = cp1252_observer
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    # Only external agent work is simulated; base-adapter task/queue, delivery
    # state, IRC framing and file observation remain real.
    async def agent_reply(event):
        calls.append(event.message_id)
        entered.set()
        await release.wait()
        return '☘ Updating Clover… current-ack'

    async with observed_adapter(observer) as (adapter, errors):
        adapter.set_message_handler(agent_reply)
        event = MessageEvent(text='valid plain text', message_id='current-input',
                             source=SessionSource(platform=Platform('irc'),
                                                  chat_id='ant', user_id='ant'))
        await adapter.handle_message(event)
        await asyncio.wait_for(entered.wait(), 2)
        assert adapter._active_sessions
        assert adapter._background_tasks
        release.set()
        while adapter._background_tasks:
            await asyncio.wait_for(asyncio.gather(*list(adapter._background_tasks)), 5)
        assert not adapter._active_sessions
        assert not adapter._pending_messages
    assert errors == [], repr(errors)
    assert calls == ['current-input']
    assert path.read_text(encoding='utf-8').count('<< PRIVMSG ant :☘ Updating Clover… current-ack') == 1

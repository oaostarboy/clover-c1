"""Native PTB serialized controls -> real callback -> isolated config readback."""
import json

import pytest
from telegram import InlineKeyboardMarkup

from clover_cli import config
from tests.test_proactive_picker import picker, query, tap, home  # noqa: F401
from tests.gateway.test_proactive_command import event


def button(markup, label):
    # Exercise real PTB JSON serialization, not a hand-built fake keyboard.
    restored = InlineKeyboardMarkup.de_json(json.loads(json.dumps(markup.to_dict())), None)
    value = next(b.callback_data for row in restored.inline_keyboard for b in row if b.text == label)
    assert isinstance(value, str)
    return value


@pytest.mark.asyncio
async def test_native_customize_save_cancel_and_old_tap_replay(picker, home):
    r, a = picker
    await r._handle_message(event('/proactive'))
    wire = a._send_message_with_thread_fallback.call_args.kwargs
    assert "First On" in wire['text'] and "suggestions" in wire['text']
    start = query(data=button(wire['reply_markup'], '⚙️ Customize'))
    await tap(a, start)
    current = start.edit_message_text.call_args.kwargs
    assert 'Save' in current['text'] and len(current['text']) < 180 and current['reply_markup'] is not None
    assert not (home / 'config.yaml').exists()
    token = button(current['reply_markup'], '🧠 Recall · Off')
    q = query(data=token)
    await tap(a, q)
    current = q.edit_message_text.call_args.kwargs
    assert 'Recall: Off' in current['text']
    q = query(data=button(current['reply_markup'], 'On'))
    await tap(a, q)
    current = q.edit_message_text.call_args.kwargs
    assert 'Recall: On' in current['text']
    replay = query(data=token)
    await tap(a, replay)
    replay.edit_message_text.assert_not_called()
    q = query(data=button(current['reply_markup'], '✖️ Cancel'))
    await tap(a, q)
    assert 'Cancelled' in q.edit_message_text.call_args.kwargs['text']
    assert not (home / 'config.yaml').exists()
    await r._handle_message(event('/proactive customize'))
    wire = a._send_message_with_thread_fallback.call_args.kwargs
    q = query(data=button(wire['reply_markup'], '✨ Use recommended'))
    await tap(a, q)
    current = q.edit_message_text.call_args.kwargs
    q = query(data=button(current['reply_markup'], '💾 Save'))
    await tap(a, q)
    assert 'Saved' in q.edit_message_text.call_args.kwargs['text']
    raw = config.require_readable_config_before_write()['proactive']
    assert raw['suggestions']['preferred_timing'] == 'after_reply'
    assert raw['suggestions']['enabled'] is True
    assert not raw.get('enabled', False)  # customize does not silently enable master


@pytest.mark.asyncio
async def test_all_native_feature_pages_and_advanced_cost_guides_keep_compact_keyboard(picker, home):
    r, a = picker
    await r._handle_message(event('/proactive customize'))
    current = a._send_message_with_thread_fallback.call_args.kwargs
    assert len(current['text']) < 180
    assert [len(row) for row in current['reply_markup'].inline_keyboard] == [2, 2, 2, 2, 2]

    async def choose(label):
        nonlocal current
        markup = current['reply_markup']
        assert all(1 <= len(row) <= 2 for row in markup.inline_keyboard)
        assert all(len(b.text) <= 28 and len(b.callback_data.encode()) <= 64
                   for row in markup.inline_keyboard for b in row)
        q = query(data=button(markup, label))
        await tap(a, q)
        current = q.edit_message_text.call_args.kwargs
        return current

    for label, snippet in [('🧠 Recall · Off', 'lexical'), ('💡 Suggestions · Off', 'No extra model'),
                           ('📚 Skill drafts · Off', 'not coordinated'), ('🧾 Context checks · Off', 'opaque')]:
        await choose(label)
        assert snippet in current['text']
        await choose('On')
        await choose('Off')
        await choose('‹ Back')
    await choose('🕓 Timing · Not set')
    await choose('Never suggest')
    await choose('‹ Back')
    await choose('⏱ Frequency · Set')
    await choose('Daily · 1/session')
    await choose('‹ Back')
    await choose('⚙️ Advanced / costs')
    assert len(current['text']) < 180
    for label, snippet in [('👀 Task monitors · Guide', 'unchanged'), ('📬 Inbox watcher · Paid', 'extra model usage'),
                           ('⚖️ Judges · Unavailable', 'not wired'), ('🛠 Repair · Unavailable', 'not wired'),
                           ('🔒 Scope / limits · Info', 'private'), ('📅 Routines · Guide', 'scheduled jobs'),
                           ('🖥 Capture · Unavailable', 'not available')]:
        await choose(label)
        assert snippet in current['text']
        assert not any('Accept' in b.text or 'Enable' in b.text
                       for row in current['reply_markup'].inline_keyboard for b in row)
        await choose('‹ Back')
    await choose('✖️ Cancel')
    assert not (home / 'config.yaml').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['revoked', 'credential', 'expired', 'foreign', 'stale'])
async def test_customize_rechecks_security_and_stale_edits_on_every_page(picker, home, bad):
    import time
    from clover_cli.proactive import proactive_command
    r, a = picker
    await r._handle_message(event('/proactive customize'))
    wire = a._send_message_with_thread_fallback.call_args.kwargs
    q = query(data=button(wire['reply_markup'], '✨ Use recommended'))
    await tap(a, q)
    wire = q.edit_message_text.call_args.kwargs
    q = query(data=button(wire['reply_markup'], '💾 Save'))
    if bad == 'revoked': r.config.platforms[next(iter(r.adapters))].extra['allow_admin_from'] = ['new-admin']
    if bad == 'credential': r._adapter_credential_fingerprint = lambda adapter: 'rotated'
    if bad == 'expired': a._choice_picker_state['c1']['guard']['expires_at'] = time.monotonic() - 1
    if bad == 'foreign': q.from_user.id = 'foreign'
    if bad == 'stale': assert 'Saved' in proactive_command('timing never', can_change=True)
    before = (home / 'config.yaml').read_bytes() if (home / 'config.yaml').exists() else None
    await tap(a, q)
    after = (home / 'config.yaml').read_bytes() if (home / 'config.yaml').exists() else None
    assert after == before
    if bad == 'stale': assert 'changed elsewhere' in q.edit_message_text.call_args.kwargs['text']
    else: q.edit_message_text.assert_not_called()

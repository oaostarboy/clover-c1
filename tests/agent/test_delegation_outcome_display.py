"""Completion display uses task evidence, not process exit or tool errors."""
import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from agent.delegation_activity import DelegationActivityTracker

pytestmark = pytest.mark.unified_card


@pytest.fixture(autouse=True)
def clean():
    ac.reset_for_tests()
    ac.set_style_for_tests('unified')
    yield
    ac.reset_for_tests()


def card_for(count=1):
    card = ac.card_for([fl.DispatchReceipt(
        delegation_id='display', seq=1, goals=tuple(f'Work {i}' for i in range(count)),
        summary='🔄 checking work → verified result', steps=('check', 'verify'),
    )])
    assert card is not None
    return card


def complete(index=0, status='completed', outcome='done', **extra):
    event = dict(delegation_id='display', task_index=index, subagent_id=f'child-{index}',
                 status=status, task_outcome={'status': outcome, 'remaining': [] if outcome == 'done' else ['verification'],
                 'owner': 'parent', 'next_step': 'review'}, **extra)
    ac.on_child_event('subagent.start', **event)
    ac.on_child_event('subagent.complete', **event)
    return event


@pytest.mark.parametrize('outcome,label,mark', [
    ('done', 'done', '🍀'), ('partial', 'partial', '☘️'),
    ('blocked', 'blocked', '☘️'), ('unknown', 'unverified', '☘️'),
])
def test_completed_task_outcome_is_not_a_failure(outcome, label, mark):
    card = card_for()
    complete(outcome=outcome)
    text = card.render()
    assert text.startswith(f'> {mark} **checking work** · {label}')
    assert 'failed' not in text
    assert card.finished


def test_six_completed_workers_keep_partial_and_blocked_counts():
    card = card_for(6)
    for i, outcome in enumerate(['done', 'done', 'partial', 'partial', 'partial', 'blocked']):
        complete(i, outcome=outcome)
    text = card.render()
    assert text.startswith('> ☘️ **6 subagents** · partial')
    assert '2 of 6 done · 3 partial · 1 blocked' in text
    assert 'failed' not in text


@pytest.mark.parametrize('status', ['stopped', 'cancelled', 'interrupted'])
def test_classic_tracker_stopped_wording_is_not_failed(status):
    tracker = DelegationActivityTracker()
    tracker.observe('subagent.complete', delegation_id='stopped-wording', status=status,
                    task_outcome={'status': 'done'})
    text = tracker.render('stopped-wording')
    assert text.startswith('⏹')
    assert 'stopped' in text
    assert 'failed' not in text
    assert tracker.snapshot('stopped-wording')[0]['state'] == 'cancelled'


@pytest.mark.parametrize('skin', ['default', 'boring'])
@pytest.mark.parametrize('workers,counts', [
    ([('completed', 'done'), ('completed', 'done'), ('completed', 'partial'),
      ('completed', 'partial'), ('completed', 'partial'), ('completed', 'blocked')],
     ['2 of 6 done', '3 partial', '1 blocked']),
    ([('completed', 'done'), ('failed', 'unknown'), ('completed', 'partial'),
      ('completed', 'blocked'), ('completed', 'unknown'), ('cancelled', 'done')],
     ['1 of 6 done', '1 failed', '1 partial', '1 blocked', '1 unverified', '1 stopped']),
])
def test_classic_ack_preserves_mixed_counts_without_a_second_card(skin, workers, counts):
    from clover_cli import skin_engine
    skin_engine.init_skin_from_config({'display': {'skin': skin}})
    ac.set_style_for_tests('classic')
    card = card_for(len(workers))
    for i, (status, outcome) in enumerate(workers):
        complete(i, status=status, outcome=outcome)
    text = card.render()
    for count in counts:
        assert count in text
    assert text.count('checking work') == 1
    assert '> ' not in text and 'tap to watch' not in text


def test_real_failure_beats_positive_task_report():
    card = card_for()
    complete(status='failed', outcome='done', summary='Plain summary: Everything looks good.')
    assert card.render().startswith('> 🥀 **checking work** · failed')


def test_classic_tracker_counts_agree_with_unified_card():
    card = card_for(6)
    tracker = DelegationActivityTracker()
    for i, outcome in enumerate(['done', 'done', 'partial', 'partial', 'partial', 'blocked']):
        event = complete(i, outcome=outcome)
        tracker.observe('subagent.complete', **event)
    text = tracker.render('display')
    assert '2 done · 3 partial · 1 blocked' in text
    assert text.startswith('☘️')
    assert 'failed' not in text


@pytest.mark.parametrize('kwargs,label,mark', [
    ({'outcome': 'unknown'}, 'unverified', '☘️'),
    ({'status': 'cancelled', 'outcome': 'done'}, 'stopped', '⏹'),
    ({'status': 'interrupted', 'outcome': 'partial'}, 'stopped', '⏹'),
    ({'status': 'completed', 'outcome': 'done', 'exit_reason': 'max_iterations'}, 'unfinished', '☘️'),
    ({'status': 'failed', 'outcome': 'partial', 'exit_reason': 'max_iterations'}, 'failed', '🥀'),
])
def test_lifecycle_precedence_and_honest_unknown(kwargs, label, mark):
    card = card_for()
    event = complete(**kwargs)
    tracker = DelegationActivityTracker()
    tracker.observe('subagent.complete', **event)
    assert card.render().startswith(f'> {mark} **checking work** · {label}')
    assert tracker.render('display').startswith(mark)


def test_missing_evidence_does_not_infer_success_from_prose():
    card = card_for()
    ac.on_child_event('subagent.complete', delegation_id='display', status='completed',
                      summary='Plain summary: All checks passed. Result is ready.')
    assert '☘️' in card.render() and 'unverified' in card.render()


def test_mixed_failed_partial_blocked_unverified_stopped_counts():
    card = card_for(6)
    for i, (status, outcome) in enumerate([
        ('completed', 'done'), ('failed', 'done'), ('completed', 'partial'),
        ('completed', 'blocked'), ('completed', 'unknown'), ('cancelled', 'done'),
    ]):
        complete(i, status=status, outcome=outcome)
    text = card.render()
    assert text.startswith('> 🥀 **6 subagents** · failed')
    for count in ['1 of 6 done', '1 failed', '1 partial', '1 blocked', '1 unverified', '1 stopped']:
        assert count in text


def test_late_completion_and_replay_do_not_double_count_or_turn_green():
    card = card_for(2)
    event = complete(0, outcome='blocked')
    before = card.render()
    assert not card.finished and '1 blocked' in before
    ac.on_child_event('subagent.complete', **event)
    assert card.render() == before
    complete(1)
    assert '1 of 2 done · 1 blocked' in card.render()
    ended = card.render()
    ac.on_child_event('subagent.complete', **{**event, 'task_outcome': {'status': 'done'}})
    assert card.render() == ended


def test_recovered_tool_error_is_not_a_worker_failure():
    card = card_for()
    for ok in [False, True]:
        ac.on_child_event('subagent.tool', delegation_id='display', tool_name='terminal',
                          tool_call_id=str(ok), args={'command': 'check'})
        ac.on_child_event('subagent.tool_done', delegation_id='display', tool_name='terminal',
                          tool_call_id=str(ok), is_error=not ok)
    complete()
    assert card.render().startswith('> 🍀') and 'failed' not in card.render()


@pytest.mark.parametrize('style', ['unified', 'classic'])
@pytest.mark.parametrize('skin', ['default', 'boring', 'tentacle-test'])
@pytest.mark.parametrize('outcome,mark', [('done', '🍀'), ('partial', '☘️'), ('blocked', '☘️'), ('unknown', '☘️')])
def test_skin_identity_and_card_style_share_marks(monkeypatch, tmp_path, style, skin, outcome, mark):
    from clover_cli import skin_engine
    ac.set_style_for_tests(style)
    if skin == 'tentacle-test':
        skins = tmp_path / 'skins'
        skins.mkdir()
        (skins / f'{skin}.yaml').write_text(
            'name: tentacle-test\nmessages:\n  mark: "🐙"\n  done_mark: "🫧"\n'
            '  fail_mark: "🦑"\n  incomplete_mark: "🪸"\n  stopped_mark: "⚓"\n')
        monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
        mark = '🫧' if outcome == 'done' else '🪸'
    skin_engine.init_skin_from_config({'display': {'skin': skin}})
    assert skin_engine.get_active_skin().name == skin
    card = card_for()
    complete(outcome=outcome)
    text = card.render()
    assert text.lstrip('> ').startswith(mark)
    label = 'unverified' if outcome == 'unknown' else outcome
    if outcome != 'done':
        assert label in text
    if skin == 'tentacle-test':
        assert not any(x in text for x in ['🍀', '🥀', '☘️', 'Clo'])


def test_custom_failed_steps_and_headers_respect_pack(monkeypatch, tmp_path):
    from clover_cli import skin_engine
    skins = tmp_path / 'skins'
    skins.mkdir()
    (skins / 'custom.yaml').write_text('name: custom\nmessages:\n  fail_mark: "🦑"\n')
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    skin_engine.init_skin_from_config({'display': {'skin': 'custom'}})
    card = card_for()
    complete(status='failed')
    assert card.render().startswith('> 🦑')
    cl = card.parts[0].checklist
    assert '🦑 check' in cl.render_unified()
    assert '🦑 check' in cl.render_classic()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,status', [('done', 'completed'), ('partial', 'completed'),
    ('blocked', 'completed'), ('unknown', 'completed'), ('done', 'failed'), ('done', 'cancelled')])
async def test_real_telegram_format_and_finalized_edit(tmp_path, outcome, status):
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock
    from gateway.config import PlatformConfig
    from gateway.delegation_activity import AckCardEditor
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='test-token'))
    adapter._rich_send_disabled = True
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=42))
    adapter._bot = bot
    adapter.edit_message = AsyncMock(wraps=adapter.edit_message)
    card = card_for()
    editor = AckCardEditor(card, adapter, '42', '42', asyncio.get_running_loop(), min_interval=0)
    card.on_change = None  # manually drive the real editor to avoid a timer race
    running = editor._render()
    assert await editor._edit(running)
    complete(status=status, outcome=outcome)
    final = editor._render()
    assert await editor._edit(final)
    assert adapter.edit_message.await_count == 2
    assert all(c.kwargs['finalize'] is True for c in adapter.edit_message.await_args_list)
    rows = []
    for raw, call in zip([running, final], bot.edit_message_text.await_args_list):
        formatted = adapter.format_message(raw)
        text = call.kwargs['text']
        assert text == formatted
        assert call.kwargs['parse_mode'] == 'MarkdownV2'
        # MarkdownV2's expandable quote opener is an intentional empty bold entity.
        assert '**' not in text.removeprefix('**>')
        assert not any(line.strip() == '>' for line in text.splitlines())
        rows.append({'raw': raw, 'formatted': text})
    (tmp_path / 'telegram-preview.json').write_text(json.dumps({
        'status': status, 'task_outcome': outcome, 'edits': rows,
        'finalize': True, 'transport': 'mocked: no live Telegram message',
    }, ensure_ascii=False, indent=2))
    editor.gone = True
    editor._unregister_live()


def test_unified_live_classic_tracker_does_not_count_unknown_as_done():
    tracker = DelegationActivityTracker()
    tracker.observe('subagent.start', delegation_id='live', subagent_id='a', task_index=0, task_count=2)
    tracker.observe('subagent.complete', delegation_id='live', subagent_id='a', task_index=0,
                    task_count=2, status='completed', task_outcome={'status': 'unknown'})
    tracker.observe('subagent.start', delegation_id='live', subagent_id='b', task_index=1, task_count=2)
    text = tracker.render('live')
    assert '1 done' not in text
    assert '☘️' in text
    assert 'failed' not in text


def test_boring_running_and_verified_done_remain_byte_identical():
    from clover_cli import skin_engine
    skin_engine.init_skin_from_config({'display': {'skin': 'boring'}})
    card = card_for()
    assert card.render() == ('🔀 **checking work**\n> Goal: verified result\n> ▸ check\n> ○ verify')
    complete()
    card.parts[0].checklist.started = None
    card.parts[0].checklist.ended = None
    assert card.render() == '> 🍀 **checking work** · done'


@pytest.mark.parametrize('batch,workers,counts,mark', [
    ('four-one-failure', [('completed', 'unknown'), ('completed', 'partial'), ('failed', 'unknown'), ('completed', 'done')],
     ['1 of 4 done', '1 failed', '1 partial', '1 unverified'], '🥀'),
    ('six-no-failures', [('completed', 'done'), ('completed', 'done'), ('completed', 'partial'), ('completed', 'partial'), ('completed', 'partial'), ('completed', 'blocked')],
     ['2 of 6 done', '3 partial', '1 blocked'], '☘️'),
    ('four-no-failures', [('completed', 'done'), ('completed', 'done'), ('completed', 'partial'), ('completed', 'blocked')],
     ['2 of 4 done', '1 partial', '1 blocked'], '☘️'),
])
def test_photographed_batch_shapes_have_truthful_counts(tmp_path, batch, workers, counts, mark):
    import json
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter
    card = card_for(len(workers))
    for i, (status, outcome) in enumerate(workers):
        complete(i, status=status, outcome=outcome)
    raw = card.render()
    assert raw.startswith(f'> {mark}')
    for count in counts:
        assert count in raw
    if not any(status == 'failed' for status, _ in workers):
        assert 'failed' not in raw
    formatted = TelegramAdapter(PlatformConfig()).format_message(raw)
    assert '**' not in formatted
    (tmp_path / 'batch-preview.json').write_text(json.dumps({
        'batch': batch, 'raw': raw, 'formatted': formatted, 'counts': counts,
    }, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('status,expected', [('failed', '🦑'), ('cancelled', '⚓')])
def test_custom_terminal_identity_in_unified_and_classic_tracker(monkeypatch, tmp_path, status, expected):
    from clover_cli import skin_engine
    skins = tmp_path / 'skins'
    skins.mkdir()
    (skins / 'sea.yaml').write_text('name: sea\nmessages:\n  done_mark: "🫧"\n  fail_mark: "🦑"\n  stopped_mark: "⚓"\n')
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    skin_engine.init_skin_from_config({'display': {'skin': 'sea'}})
    card = card_for()
    event = complete(status=status)
    tracker = DelegationActivityTracker()
    tracker.observe('subagent.complete', **event)
    assert card.render().startswith('> ' + expected)
    assert tracker.render('display').startswith(expected)
    assert not any(x in card.render() for x in ['🍀', '🥀', 'Clo'])

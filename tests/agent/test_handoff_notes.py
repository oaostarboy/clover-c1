"""The safety-net hand-off gives the helper the whole turn's context.

* ``agent.handoff_notes`` (pure part): the bounded work log, the notes file
  (0600 in a 0700 dir, redacted, newest kept), the cleanup, the "known so far"
  call that is silent on failure, and the agent's own voice line (``on it`` when it cannot be had).
* ``agent.router_backstop``: both paths (planned + direct) write the notes at
  hand-off, put the path and the bullets in the helper's context (under the
  cap), and ``delegation.router.handoff_notes: false`` restores the old context.

Real ``AIAgent._execute_tool_calls``, real tools, real ``delegate_task`` handler
and child pool (the helper's model conversation is faked); the two short model
calls are faked at the auxiliary-client boundary.
"""
from __future__ import annotations

import os
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from agent import delegation_router as dr
from agent import handoff_notes as hn
from agent import router_backstop as rb
from tests.agent.test_delegation_checkpoint import run_batch
from tests.agent.test_router_backstop import (  # noqa: F401  (fixtures)
    _clean_registry,
    _root,
    _wait_started,
    _write,
    children,
    clock,
    dispatches,
)

pytestmark = pytest.mark.real_delegation_router

REQUEST = 'Email the existing recipient the AutoHDR extension, with the instructions I gave you.'
DIRECT = dr.Verdict(mode='direct', tasks=(), reason='one send after a quick look')
SECRET = 'sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF'


@pytest.fixture(autouse=True)
def _fake_calls(monkeypatch):
    """The two short model calls, faked at the auxiliary-client boundary."""
    import agent.auxiliary_client as ac

    box = SimpleNamespace(calls=[], reply='- Found the recipient: Julia <julia@example.com>.\n'
                                          '- Two files were written.\n- Still to do: send the mail.',
                          error=None, delay=0.0)

    def fake(task=None, *, messages=None, **kw):
        box.calls.append({'task': task, 'messages': messages, **kw})
        if box.delay:
            time.sleep(box.delay)
        if box.error:
            raise box.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=box.reply))])

    monkeypatch.setattr(ac, 'call_llm', fake)
    return box


@pytest.fixture
def aux_box(_fake_calls):
    return _fake_calls


def _history():
    return [
        {'role': 'user', 'content': 'Who did I email about the HDR plugin last week?'},
        {'role': 'assistant', 'content': 'You emailed Julia <julia@example.com> on Tuesday.'},
    ]


def _arm(agent, *, settings=None, history=None, verdict=DIRECT, message=REQUEST):
    rb.begin(agent, verdict, message, settings or dr.RouterSettings(enabled=True, direct_backstop_calls=3),
             history=history if history is not None else _history())


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _path_in(context: str) -> str:
    line = [ln for ln in context.splitlines() if ln.startswith('Full handoff notes (read this first): ')][0]
    return line.split(': ', 1)[1].strip()


# ── the work log: full results, bounded, newest kept ───────────────────────

class TestWorkLog:
    def test_full_results_are_kept_not_cut_at_record_time(self):
        log = hn.WorkLog()
        big = 'line\n' * 2000  # 10,000 chars: far past the old 1,200 per finding
        log.add('read_file', '{"path": "a"}', big)
        assert log.entries[0].result == big and log.trimmed == 0

    def test_a_result_over_the_cap_keeps_head_and_tail(self):
        log = hn.WorkLog(result_chars=1000)
        log.add('terminal', '{}', 'HEAD' + 'x' * 5000 + 'TAIL')
        out = log.entries[0].result
        assert out.startswith('HEAD') and out.endswith('TAIL') and 'chars cut here' in out
        assert len(out) < 1200 and log.trimmed == 1

    def test_the_total_cap_drops_the_oldest_and_keeps_the_newest(self):
        log = hn.WorkLog(result_chars=1000, total_chars=3500)
        for i in range(10):
            log.add('read_file', f'{{"n": {i}}}', f'{i}' * 900)
        names = [e.args for e in log.entries]
        assert names[-1] == '{"n": 9}' and '{"n": 0}' not in names
        assert log.dropped == 10 - len(log.entries) and log.seen == 10
        assert log.chars <= 3500 + 900

    def test_the_caps_are_the_documented_ones(self):
        assert (hn.RESULT_CHARS, hn.TOTAL_CHARS) == (20_000, 200_000)


class TestWhatWasTouched:
    @pytest.mark.parametrize('name, args, want', [
        ('write_file', {'path': '/tmp/a.txt', 'content': 'x'}, ['/tmp/a.txt']),
        ('patch', {'path': '/repo/x.py', 'old_string': 'a', 'new_string': 'b'}, ['/repo/x.py']),
        ('patch', {'mode': 'patch', 'patch': '*** Begin Patch\n*** Update File: a/b.py\n*** Add File: c.py\n'},
         ['a/b.py', 'c.py']),
        ('terminal', {'command': 'echo hi > /tmp/out.txt && cat /tmp/out.txt'}, ['/tmp/out.txt']),
        ('terminal', {'command': 'ls 2>&1 | tee -a /tmp/log.txt'}, ['/tmp/log.txt']),
        ('terminal', {'command': 'ls -la /tmp'}, []),
        ('terminal', {'command': 'make 2>/dev/null'}, []),
        ('read_file', {'path': '/tmp/a.txt'}, []),
    ])
    def test_files_written(self, name, args, want):
        assert hn.files_touched(name, args) == want

    def test_skill_opened(self):
        assert hn.skill_opened('skill_view', {'name': 'google-workspace'}) == 'google-workspace'
        assert hn.skill_opened('read_file', {'name': 'x'}) == ''


# ── the earlier conversation ───────────────────────────────────────────────

class TestRecentTurns:
    def test_last_eight_text_turns_oldest_first_each_capped(self):
        hist = []
        for i in range(12):
            hist.append({'role': 'user', 'content': f'question {i}'})
            hist.append({'role': 'assistant', 'content': f'answer {i} ' + 'z' * 5000})
        turns = hn.recent_turns(hist)
        assert len(turns) == hn.HISTORY_TURNS == 8
        assert turns[0][1].startswith('question 8') and turns[-1][1].startswith('answer 11')
        assert all(len(text) <= hn.HISTORY_TURN_CHARS + 2 for _, text in turns)

    def test_text_only_no_tool_rows_no_hidden_scaffolding_no_memory_blocks(self):
        hist = [
            {'role': 'user', 'content': 'hello there'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c', 'function': {'name': 'x'}}]},
            {'role': 'tool', 'content': 'RAW TOOL OUTPUT', 'tool_call_id': 'c'},
            {'role': 'system', 'content': 'SYSTEM PROMPT TEXT'},
            {'role': 'user', 'content': 'summarize now', 'display_kind': 'hidden'},
            {'role': 'assistant', 'content': 'hi <memory-context>SECRET MEMORY</memory-context>there'},
        ]
        turns = hn.recent_turns(hist)
        blob = ' '.join(t for _, t in turns)
        assert [r for r, _ in turns] == ['user', 'assistant']
        assert 'RAW TOOL OUTPUT' not in blob and 'SYSTEM PROMPT' not in blob
        assert 'summarize now' not in blob and 'SECRET MEMORY' not in blob

    def test_list_content_is_flattened(self):
        turns = hn.recent_turns([{'role': 'user', 'content': [{'type': 'text', 'text': 'see this'},
                                                              {'type': 'image_url', 'image_url': {}}]}])
        assert turns == [('user', 'see this')]


# ── the notes file ─────────────────────────────────────────────────────────

class TestNotesFile:
    def _render(self, **kw):
        log = hn.WorkLog()
        log.add('read_file', '{"path": "/a"}', 'FULL RESULT ONE')
        log.add('web_search', '{"q": "x"}', 'FULL RESULT TWO')
        args = dict(request=REQUEST, history=[('user', 'who?'), ('assistant', 'Julia.')], log=log,
                    files=['/tmp/a.txt'], skills=['google-workspace'],
                    todos=['- [x] find recipient', '- [ ] send'], running=['- terminal({"command": "sleep"})'])
        args.update(kw)
        return hn.render_notes(**args)

    def test_sections_come_in_the_documented_order(self):
        text = self._render()
        order = [text.index(h) for h in (
            "## The user's request (verbatim)", '## Recent conversation', "## This turn's work so far",
            '## Files written or edited', '## Skills the parent opened', "## The parent's todo list",
            '## Still running')]
        assert order == sorted(order)
        assert text.index('FULL RESULT ONE') < text.index('FULL RESULT TWO')
        assert 'Julia.' in text and '/tmp/a.txt' in text and 'google-workspace' in text and '[x] find recipient' in text

    def test_a_dropped_or_trimmed_log_says_so(self):
        log = hn.WorkLog(result_chars=100, total_chars=250)
        for i in range(6):
            log.add('t', '{}', 'r' * 200)
        text = self._render(log=log)
        assert 'oldest call(s) are left out' in text and 'cut in the middle' in text

    def test_file_is_0600_in_a_0700_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hn, 'notes_dir', lambda: tmp_path / 'cache' / 'handoff')
        path = hn.write_notes('deleg_abc12345', 'hello')
        assert path == tmp_path / 'cache' / 'handoff' / 'deleg_abc12345.md'
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        assert _read(path) == 'hello\n' or _read(path) == 'hello'

    def test_secrets_are_redacted_before_the_file_is_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hn, 'notes_dir', lambda: tmp_path / 'h')
        path = hn.write_notes('deleg_abc12345', f'the key is {SECRET} and OPENAI_API_KEY={SECRET}')
        body = _read(path)
        assert SECRET not in body

    @pytest.mark.parametrize('bad', ['', '../etc/passwd', 'a/b', 'x' * 200, 'sp ace'])
    def test_an_unsafe_delegation_id_writes_nothing(self, tmp_path, monkeypatch, bad):
        monkeypatch.setattr(hn, 'notes_dir', lambda: tmp_path / 'h')
        assert hn.write_notes(bad, 'x') is None
        assert not (tmp_path / 'h').exists() or not list((tmp_path / 'h').iterdir())

    def test_cleanup_removes_only_files_older_than_seven_days(self, tmp_path, monkeypatch):
        d = tmp_path / 'h'
        d.mkdir()
        monkeypatch.setattr(hn, 'notes_dir', lambda: d)
        old, fresh = d / 'deleg_old.md', d / 'deleg_new.md'
        old.write_text('o')
        fresh.write_text('n')
        past = time.time() - 8 * 86400
        os.utime(old, (past, past))
        assert hn.cleanup_old() == 1
        assert not old.exists() and fresh.exists()

    def test_cleanup_with_no_directory_is_a_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hn, 'notes_dir', lambda: tmp_path / 'nope')
        assert hn.cleanup_old() == 0

    def test_the_default_location_is_under_clover_home_cache_handoff(self):
        from clover_constants import get_clover_home

        assert hn.notes_dir() == get_clover_home() / 'cache' / 'handoff'


# ── "what's known so far": one cheap call, silent on failure ───────────────

class TestKnownSoFar:
    def test_it_uses_the_routers_small_model_task_and_returns_the_bullets(self, aux_box):
        bullets = hn.known_so_far('NOTES TEXT')
        assert bullets == ['Found the recipient: Julia <julia@example.com>.', 'Two files were written.',
                           'Still to do: send the mail.']
        (call,) = aux_box.calls
        assert call['task'] == hn.SUMMARY_TASK == 'delegation_router'
        assert call['timeout'] == hn.KNOWN_TIMEOUT_SECONDS
        assert 'NOTES TEXT' in call['messages'][1]['content']

    def test_notes_over_30k_are_trimmed_head_and_tail(self, aux_box):
        notes = 'HEAD-MARK ' + 'a' * 100_000 + ' TAIL-MARK'
        hn.known_so_far(notes)
        sent = aux_box.calls[0]['messages'][1]['content']
        assert len(sent) < hn.SUMMARY_INPUT_CHARS + 500 and 'HEAD-MARK' in sent and 'TAIL-MARK' in sent

    def test_a_timeout_is_silent(self, aux_box):
        aux_box.delay = 1.0
        started = time.monotonic()
        assert hn.known_so_far('n', timeout=0.1) == []
        assert time.monotonic() - started < 0.8

    def test_an_error_is_silent(self, aux_box):
        aux_box.error = RuntimeError('provider down')
        assert hn.known_so_far('n') == []

    @pytest.mark.parametrize('reply', ['', 'no bullets here at all', None])
    def test_an_unusable_reply_gives_no_bullets(self, aux_box, reply):
        aux_box.reply = reply
        assert hn.known_so_far('n') == []

    def test_at_most_six_bullets_each_capped(self):
        text = '\n'.join(f'- bullet {i} ' + 'w' * 600 for i in range(10))
        out = hn.parse_bullets(text)
        assert len(out) == 6 and all(len(b) <= hn.SUMMARY_BULLET_CHARS for b in out)

    def test_numbered_and_star_bullets_are_accepted(self):
        assert hn.parse_bullets('1. one\n* two\n• three') == ['one', 'two', 'three']


# ── the opener line ────────────────────────────────────────────────────────

class TestVoiceLineFallback:
    """No canned wording: the line is the agent's own, or ``on it``."""

    def test_the_old_template_is_gone(self):
        assert not hasattr(hn, 'fallback_opener') and not hasattr(hn, 'opener_line')
        assert hn.FALLBACK_LINE == 'on it'

    def test_no_transcript_means_on_it_never_the_title(self):
        line = hn.voice_line(SimpleNamespace(), [], 'fixing the bug')
        assert line == 'on it' and 'fixing' not in line and 'bigger than it looked' not in line

    def test_an_error_means_on_it(self):
        class Boom(SimpleNamespace):
            pass

        agent = SimpleNamespace()  # no model client: the turn raises
        msgs = [{'role': 'user', 'content': 'go'}, {'role': 'assistant', 'content': 'ok'}]
        assert hn.voice_line(agent, msgs, 'fixing the bug') == 'on it'

    @pytest.mark.parametrize('text', ['DONE', '', 'ok', 'Sure, running into the safety net now for you',
                                      'handing it off after the time limit ran out'])
    def test_an_unusable_or_leaky_line_is_dropped(self, text):
        assert hn._clean_opener(text) == ''

    def test_a_good_line_is_cleaned_and_capped(self):
        assert hn._clean_opener('"handing this to a subagent, it is digging into the fuse now"') == (
            'handing this to a subagent, it is digging into the fuse now')
        assert len(hn._clean_opener('word ' * 100)) <= hn.OPENER_CHARS + 1


class TestSettledMessages:
    def test_an_unfinished_tool_batch_is_cut_off_the_copy(self):
        msgs = [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'a', 'function': {'name': 'x'}},
                                                                {'id': 'b', 'function': {'name': 'x'}}]},
            {'role': 'tool', 'tool_call_id': 'a', 'content': 'ok'},
        ]
        assert [m['role'] for m in hn.settled_messages(msgs)] == ['user']

    def test_a_finished_batch_is_kept_and_the_input_is_never_changed(self):
        msgs = [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'a', 'function': {'name': 'x'}}]},
            {'role': 'tool', 'tool_call_id': 'a', 'content': 'ok'},
        ]
        before = list(msgs)
        assert len(hn.settled_messages(msgs)) == 3
        assert msgs == before


# ── the backstop: the notes ride the hand-off (direct path) ────────────────

def _trip(agent, tmp_path, n=3):
    """``n`` work calls finish one after the other, then the next one is refused
    and the runtime hands off (a batch would leave them all "still running")."""
    results = []
    for i in range(n + 1):
        results += run_batch(agent, [_write(tmp_path, f'{i}.txt')])
    return results


def test_direct_handoff_writes_the_full_notes_and_the_context_points_at_them(
        tmp_path, children, dispatches, aux_box):
    agent = _root()
    _arm(agent)

    _trip(agent, tmp_path)
    _wait_started(children, 1)

    context = dispatches[0]['tasks'][0]['context']
    path = _path_in(context)
    assert os.path.isabs(path) and os.path.isfile(path)
    assert os.path.basename(path) == rb._state(agent).handoff['delegation_id'] + '.md'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    notes = _read(path)
    assert REQUEST in notes
    assert 'Who did I email about the HDR plugin last week?' in notes
    assert 'You emailed Julia <julia@example.com> on Tuesday.' in notes
    for i in range(3):
        assert f'{i}.txt' in notes, 'every work call, with its args and result'
    assert notes.count('### ') == 3
    # bullets + the read-first instruction are in the helper's prompt
    assert 'Known so far' in context and 'Found the recipient: Julia' in context
    assert 'read_file' in context and 'before you search' in context
    assert len(context) <= rb.CONTEXT_CHARS
    # the old digest is still there (cheap)
    assert 'Findings already gathered' in context
    # and the helper really received it
    blob = children[0]['system'] + str(children[0]['user'])
    assert path in blob


def test_files_skills_and_todos_are_listed(tmp_path, children, dispatches, aux_box):
    agent = _root()
    agent._todo_store.write([{'id': '1', 'content': 'find the recipient', 'status': 'completed'},
                             {'id': '2', 'content': 'send the mail', 'status': 'in_progress'}])
    _arm(agent)
    rb.note_result(agent, 'skill_view', {'name': 'google-workspace'}, '{"success": true}')
    rb.note_result(agent, 'skill_view', {'name': 'google-workspace'}, '{"success": true}')
    rb.note_result(agent, 'terminal', {'command': 'echo hi > /tmp/handoff-out.txt'}, 'hi')

    _trip(agent, tmp_path)

    notes = _read(_path_in(dispatches[0]['tasks'][0]['context']))
    assert '## Files written or edited this turn' in notes
    assert str(tmp_path / '0.txt') in notes and '/tmp/handoff-out.txt' in notes
    assert notes.count('- google-workspace') == 1, 'listed once'
    assert '[x] find the recipient' in notes and '[>] send the mail' in notes
    context = dispatches[0]['tasks'][0]['context']
    assert 'Load the same skills the parent opened' in context and 'google-workspace' in context


def test_a_skill_open_is_not_a_work_call(tmp_path, children, dispatches, aux_box):
    agent = _root()
    _arm(agent)
    rb.note_result(agent, 'skill_view', {'name': 'x-skill'}, 'body of the skill')
    assert rb._state(agent).work_log.seen == 0 and rb._state(agent).skills == ['x-skill']


def test_secrets_in_tool_results_never_reach_the_notes_or_the_summary_call(
        tmp_path, children, dispatches, aux_box):
    agent = _root()
    _arm(agent)
    rb.note_result(agent, 'read_file', {'path': '/home/u/.env'}, f'OPENAI_API_KEY={SECRET}\nOTHER=1')

    _trip(agent, tmp_path)

    notes = _read(_path_in(dispatches[0]['tasks'][0]['context']))
    assert SECRET not in notes
    assert all(SECRET not in str(c['messages']) for c in aux_box.calls)


def test_results_are_full_in_the_file_but_cut_in_the_digest(tmp_path, children, dispatches, aux_box):
    agent = _root()
    _arm(agent)
    big = 'BEGIN ' + 'detail ' * 1500 + ' END-OF-RESULT'
    rb.note_result(agent, 'read_file', {'path': '/big'}, big)

    _trip(agent, tmp_path)

    context = dispatches[0]['tasks'][0]['context']
    notes = _read(_path_in(context))
    assert big.strip() in notes
    assert 'END-OF-RESULT' not in context, 'the in-prompt digest is still the short one'


def test_the_summary_call_gets_the_notes_and_runs_on_the_router_task(tmp_path, children, dispatches, aux_box):
    agent = _root()
    _arm(agent)
    _trip(agent, tmp_path)
    summary = [c for c in aux_box.calls if c['task'] == 'delegation_router']
    # two small calls: the tiny title one and the big known-so-far one
    assert len(summary) == 2
    known = [c for c in summary if c['messages'][0]['content'].startswith('You brief a colleague')]
    title = [c for c in summary if c['messages'][0]['content'].startswith('You name a half-finished job')]
    assert len(known) == 1 and len(title) == 1
    sent = known[0]['messages'][1]['content']
    assert REQUEST in sent and 'Who did I email' in sent and '0.txt' in sent
    assert len(title[0]['messages'][1]['content']) < len(sent)


def test_a_summary_timeout_still_hands_off_with_the_path_and_no_bullets(
        tmp_path, children, dispatches, aux_box, monkeypatch):
    monkeypatch.setattr(hn, 'KNOWN_TIMEOUT_SECONDS', 0.2)
    monkeypatch.setattr(hn, 'TITLE_TIMEOUT_SECONDS', 0.2)
    monkeypatch.setattr(hn, 'VOICE_TIMEOUT_SECONDS', 0.2)
    aux_box.delay = 1.0
    agent = _root()
    _arm(agent)

    results = _trip(agent, tmp_path)

    assert any(isinstance(r, dict) and r.get('error_type') == rb.HANDOFF_CODE for r in results)
    context = dispatches[0]['tasks'][0]['context']
    assert 'Full handoff notes (read this first):' in context
    assert 'Known so far' not in context
    text = rb._ack_text(rb.turn_receipts(agent), agent).lower()
    assert 'timed out' not in text and 'timeout' not in text


def test_a_summary_error_is_silent_and_the_handoff_goes_on(tmp_path, children, dispatches, aux_box):
    aux_box.error = RuntimeError('boom')
    agent = _root()
    _arm(agent)
    _trip(agent, tmp_path)
    context = dispatches[0]['tasks'][0]['context']
    assert 'Full handoff notes' in context and 'Known so far' not in context and 'boom' not in context


def test_the_context_stays_under_the_cap_with_a_huge_request_and_many_findings(
        tmp_path, children, dispatches, aux_box):
    aux_box.reply = '\n'.join(f'- bullet {i} ' + 'w' * 400 for i in range(6))
    agent = _root()
    _arm(agent, message='please ' + 'do the thing ' * 1500)
    for i in range(20):
        rb.note_result(agent, 'read_file', {'path': f'/f{i}'}, 'x' * 3000)
    _trip(agent, tmp_path)
    context = dispatches[0]['tasks'][0]['context']
    assert len(context) <= rb.CONTEXT_CHARS == 7800
    assert 'Full handoff notes (read this first):' in context, 'the path is never what gets cut'
    assert os.path.isfile(_path_in(context))


def test_the_flag_off_is_the_old_context_and_writes_no_file(tmp_path, children, dispatches, aux_box):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=3, handoff_notes=False))

    _trip(agent, tmp_path)

    context = dispatches[0]['tasks'][0]['context']
    assert 'Full handoff notes' not in context and 'Known so far' not in context
    assert 'Findings already gathered' in context
    assert '_reserved_delegation_id' not in dispatches[0]
    assert rb._state(agent).work_log is None
    assert not list(hn.notes_dir().glob('*.md')) if hn.notes_dir().exists() else True
    assert [c for c in aux_box.calls if c['task'] == 'delegation_router'] == []


def test_the_setting_defaults_on_and_reads_from_config(monkeypatch):
    monkeypatch.setenv('CLOVER_IGNORE_USER_CONFIG', '1')
    assert dr.load_settings().handoff_notes is True
    from clover_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG['delegation']['router']['handoff_notes'] is True
    monkeypatch.delenv('CLOVER_IGNORE_USER_CONFIG')
    import clover_cli.config as cfg

    monkeypatch.setattr(cfg, 'load_config_readonly',
                        lambda: {'delegation': {'router': {'handoff_notes': False}}})
    assert dr.load_settings().handoff_notes is False


def test_old_notes_are_cleaned_at_the_next_handoff(tmp_path, children, dispatches, aux_box):
    d = hn.notes_dir()
    d.mkdir(parents=True, exist_ok=True)
    stale = d / 'deleg_stale000.md'
    stale.write_text('old')
    past = time.time() - 9 * 86400
    os.utime(stale, (past, past))
    agent = _root()
    _arm(agent)
    _trip(agent, tmp_path)
    assert not stale.exists()
    assert os.path.isfile(_path_in(dispatches[0]['tasks'][0]['context']))


# ── the planned path ───────────────────────────────────────────────────────

def test_the_planned_path_writes_the_notes_too(tmp_path, children, dispatches, aux_box):
    agent = _root()
    verdict = dr.Verdict(mode='single', reason='Email the recipient the extension.',
                         tasks=(dr.PlanTask(goal='Email the existing recipient', acceptance='sent'),))
    rb.begin(agent, verdict, REQUEST, dr.RouterSettings(enabled=True, backstop_calls=2, backstop_seconds=None),
             history=_history())
    rb.note_result(agent, 'read_file', {'path': '/contacts'}, 'FULL CONTACT LIST ' * 200)

    run_batch(agent, [_write(tmp_path, 'a.txt'), _write(tmp_path, 'b.txt'), _write(tmp_path, 'c.txt')])

    context = dispatches[0]['tasks'][0]['context']
    notes = _read(_path_in(context))
    assert ('FULL CONTACT LIST ' * 200).strip() in notes and 'You emailed Julia' in notes
    assert 'Plan: single' in context and 'Known so far' in context


def test_a_staged_plan_carries_the_notes_path_to_the_next_stage(tmp_path, monkeypatch):
    notes = tmp_path / 'n.md'
    notes.write_text('x')
    verdict = dr.Verdict(mode='staged', tasks=(dr.PlanTask(goal='a' * 12), dr.PlanTask(goal='b' * 12, depends_on=(0,))))
    pseudo = SimpleNamespace(verdict=verdict, user_message='req', notes_path=rb._still_there(str(notes)))
    assert 'Full handoff notes (read this first): ' + str(notes) in rb._helper_context(pseudo, 1, 1, '')
    gone = SimpleNamespace(verdict=verdict, user_message='req', notes_path=rb._still_there(str(tmp_path / 'gone.md')))
    assert 'Full handoff notes' not in rb._helper_context(gone, 1, 1, '')


# ── a failed dispatch leaves nothing behind ────────────────────────────────

def test_a_refused_dispatch_removes_the_notes(tmp_path, children, monkeypatch, aux_box):
    import agent.tool_executor as te

    monkeypatch.setattr(te, 'dispatch_generated_tool',
                        lambda agent, name, args, task_id='': '{"error": "denied by a plugin"}')
    agent = _root()
    _arm(agent)
    results = _trip(agent, tmp_path)
    assert any(isinstance(r, dict) and r.get('error_type') == rb.FAILED_CODE for r in results)
    assert not list(hn.notes_dir().glob('*.md')) if hn.notes_dir().exists() else True
    assert rb._state(agent).notes_path == ''


# ── the delegation id is the runtime's, not the model's ────────────────────

class TestReservedIds:
    def test_only_a_reserved_id_is_honoured_and_only_once(self):
        did = hn.reserve_delegation_id()
        assert hn.consume_reserved(did) == did
        assert hn.consume_reserved(did) is None
        assert hn.consume_reserved('deleg_madeup1') is None
        assert hn.consume_reserved(None) is None

    def test_a_model_supplied_argument_is_not_in_the_schema(self):
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA, RESERVED_ID_ARG

        assert RESERVED_ID_ARG not in str(DELEGATE_TASK_SCHEMA)


# ── the opener rides the hand-off as the root's own line ───────────────────

def test_the_dispatch_carries_no_canned_opener_and_the_agent_voices_it_after(
        tmp_path, children, dispatches, aux_box):
    """The line is written AFTER the helper is accepted (one more model turn),
    never pre-written into the dispatch."""
    agent = _root()
    _arm(agent)
    _trip(agent, tmp_path)
    assert 'say' not in dispatches[0]['handoff']
    directive = rb.completion_directive(agent)
    assert directive is not None
    say = directive.text
    assert say.startswith('on it'), 'no transcript here, so the neutral line'
    for word in ('limit', 'timeout', 'timed out', 'safety', 'backstop', 'bigger than it looked'):
        assert word not in say.lower()
    assert REQUEST not in say


def test_the_hand_back_of_a_finished_helper_keeps_its_own_wording(tmp_path, children, dispatches, aux_box):
    agent = _root()
    verdict = dr.Verdict(mode='single', source='lifecycle', reason='x',
                         tasks=(dr.PlanTask(goal='Check the helper results', acceptance='checked'),))
    rb.begin(agent, verdict, 'check these', dr.RouterSettings(enabled=True, backstop_calls=1, backstop_seconds=None))
    run_batch(agent, [_write(tmp_path, 'a.txt'), _write(tmp_path, 'b.txt')])
    assert 'say' not in dispatches[0]['handoff']
    assert 'Full handoff notes' not in dispatches[0]['tasks'][0]['context']


# ── concurrency: the log is safe under a parallel batch ────────────────────

def test_results_noted_from_several_threads_are_all_kept(tmp_path):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=500))
    threads = [threading.Thread(target=rb.note_result, args=(agent, 'read_file', {'path': f'/f{i}'}, f'r{i}'))
               for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert rb._state(agent).work_log.seen == 40


# ── the opener is its own message and the card binds under it ──────────────

@pytest.mark.unified_card
@pytest.mark.asyncio
async def test_unified_style_the_reply_is_the_opener_alone_and_the_card_binds_under_it(
        tmp_path, children, dispatches, aux_box):
    """The user-visible fix: the turn used to post only the card.  Now the reply
    is the agent's own line, and the gateway's ``bind_ack_card`` finds the card
    through ``match_unbound_opener`` (an exact match on that line) and sends it
    as a second message right under it."""
    import asyncio

    from agent import ack_checklist as ac
    from gateway import delegation_activity as da
    from gateway.platforms.base import SendResult

    ac.reset_for_tests()
    da._ACK_EDITORS.clear()
    try:
        agent = _root(platform='telegram')
        _arm(agent)
        await asyncio.to_thread(_trip, agent, tmp_path)

        directive = rb.completion_directive(agent)
        say = 'on it'
        assert directive is not None and directive.text == say, 'the reply is the opener, nothing else'
        assert '\U0001f504' not in directive.text

        class Adapter:
            sends = []

            async def send(self, chat_id, content, reply_to=None, metadata=None):
                self.sends.append(content)
                return SendResult(success=True, message_id=f'm{len(self.sends)}')

            async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
                return SendResult(success=True, message_id=message_id)

        adapter = Adapter()
        sent = await adapter.send('chat', directive.text)  # the gateway delivers the reply
        pending = da.bind_ack_card(adapter, 'chat', directive.text, sent)
        assert pending is not None, 'the opener alone is bound: the card is sent under it'
        await pending
        assert adapter.sends[0] == say
        assert '**emailing the existing recipient' in adapter.sends[1], 'the card names the job'
        assert say not in adapter.sends[1], 'the card is its own message'
        assert len(adapter.sends) == 2
        # a later, different reply does not claim the card
        assert ac.match_unbound_opener('some other reply that merely mentions a subagent') is None
    finally:
        da._ACK_EDITORS.clear()
        ac.reset_for_tests()

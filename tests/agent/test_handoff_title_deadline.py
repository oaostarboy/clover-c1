"""The hand-off card's title must not wait on the slow "known so far" call, and
must come from the session when the small model is too slow.

Live bug (2026-10-13, deleg_25890b3b): one small-model call made the title,
goal AND the "known so far" bullets from a 26k-char notes file.  It needs ~8 s
and was capped at 6 s, so it timed out 3 times out of 3; the card then said
"finishing this up" although the user's "1. ok, do it. 2. ok. 3. ye. Yes."
answered the assistant's own offer ("want me to start on the tracking?").

Behaviour pinned here (real ``router_backstop`` machinery, real delegate
handler; only the auxiliary model call is faked):

* a title model slower than its deadline: the title still names the work, from
  the assistant's last offer / the chat's last hand-off, never the placeholder;
* a slow bullets call does not delay the dispatch or the title; the bullets are
  written into the notes file when they land;
* the placeholder is shown only when there is truly nothing.
"""
from __future__ import annotations

import time
import threading
from types import SimpleNamespace

import pytest

from agent import delegation_router as dr
from agent import handoff_notes as hn
from agent import handoff_title as ht
from agent import router_backstop as rb
from tests.agent.test_handoff_notes import (  # noqa: F401  (fixtures + helpers)
    DIRECT,
    _path_in,
    _read,
    _trip,
)
from tests.agent.test_router_backstop import (  # noqa: F401
    _clean_registry,
    _root,
    _wait_started,
    children,
    clock,
    dispatches,
)

pytestmark = pytest.mark.real_delegation_router

GO_AHEAD = '1. ok, do it.\n\n2. ok.\n\n3. ye.\n\nYes.'
OFFER = ("tldr: ads are off, nothing's costing you money. i fix the tracking quietly, no banner. "
         "relaunch when you're ready.\n\nwant me to start on the tracking?")
HISTORY = [
    {'role': 'user', 'content': '1. But it would put an ugly banner, no? I don’t want that.'},
    {'role': 'assistant', 'content': OFFER},
]
TITLE_JSON = '{"title": "adding Google Ads quote tracking", "goal": "quote requests counted as conversions"}'
KNOWN_JSON = '{"known": ["The site has no Google Ads tag.", "Plan: carry the gclid through the quote form."]}'


@pytest.fixture
def small_model(monkeypatch):
    """Script calls with explicit release events and own all worker lifetimes."""
    import agent.auxiliary_client as ac

    box = SimpleNamespace(calls=[], timeouts=[],
                          entered={k: threading.Event() for k in ('title', 'known')},
                          release={k: threading.Event() for k in ('title', 'known')},
                          reply={'title': TITLE_JSON, 'known': KNOWN_JSON})
    for event in box.release.values():
        event.set()
    workers = []
    start = threading.Thread.start

    def owned_start(worker, *args, **kwargs):
        if worker.name in {'handoff-known', 'handoff-notes-call'}:
            workers.append(worker)
        return start(worker, *args, **kwargs)

    def fake(task=None, *, messages=None, **kw):
        system = str(messages[0]['content'])
        kind = ('title' if system.startswith('You name a half-finished job')
                else 'known' if system.startswith('You brief a colleague') else 'other')
        box.calls.append({'kind': kind, 'task': task, 'messages': messages, **kw})
        if kind in box.entered:
            box.entered[kind].set()
            assert box.release[kind].wait(10), 'test did not release scripted auxiliary call'
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=box.reply.get(kind, '{}')))])

    timed_call = hn._timed_call

    def observed_timeout(kwargs, timeout):
        try:
            return timed_call(kwargs, timeout)
        except TimeoutError:
            box.timeouts.append(timeout)
            raise

    monkeypatch.setattr(threading.Thread, 'start', owned_start)
    monkeypatch.setattr(hn, '_timed_call', observed_timeout)
    monkeypatch.setattr(ac, 'call_llm', fake)
    yield box
    # Timed-out model calls and late note writers belong to this fixture.
    # Release and join before monkeypatch removes their external boundary.
    for event in box.release.values():
        event.set()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive(), 'scripted handoff worker outlived its fixture'


def _arm_chat(agent, *, message=GO_AHEAD, history=HISTORY, session='chat-deadline'):
    agent.session_id = session
    rb.begin(agent, DIRECT, message, dr.RouterSettings(enabled=True, direct_backstop_calls=3), history=history)


@pytest.fixture
def short_deadlines(monkeypatch):
    monkeypatch.setattr(hn, 'TITLE_TIMEOUT_SECONDS', 0.3)
    monkeypatch.setattr(hn, 'KNOWN_GRACE_SECONDS', 0.1)


def test_a_title_model_past_its_deadline_still_gives_the_offered_work(
        tmp_path, children, dispatches, small_model, short_deadlines):
    small_model.release['title'].clear()  # slower than the deadline: the brief is lost
    agent = _root()
    _arm_chat(agent)

    _trip(agent, tmp_path)

    work = dispatches[0]['handoff']['work']
    assert work != ht.FALLBACK_TITLE
    assert work == 'starting on the tracking', 'the go-ahead answers the assistant\'s own offer'
    assert dispatches[0]['handoff']['outcome'] != ht.FALLBACK_GOAL
    assert small_model.entered['title'].is_set()
    assert not small_model.release['title'].is_set(), 'dispatch returned before the model was released'
    assert small_model.timeouts == [hn.TITLE_TIMEOUT_SECONDS], 'real model deadline fired'


def test_a_slow_title_model_falls_back_to_the_chats_last_real_handoff(
        tmp_path, children, dispatches, small_model, short_deadlines):
    small_model.release['title'].clear()
    hn.remember_title('chat-prior', 'adding Google Ads quote tracking')
    agent = _root()
    # no offer to answer in the history, and the agent object is a fresh one
    # (a restart lost ``_handoff_last_title``): the durable titles still know
    _arm_chat(agent, history=[{'role': 'user', 'content': 'hello there'}], session='chat-prior')

    _trip(agent, tmp_path)

    work = dispatches[0]['handoff']['work']
    assert work != ht.FALLBACK_TITLE
    assert 'google ads quote tracking' in work.lower()


def test_the_placeholder_is_a_last_resort_when_there_is_truly_nothing(
        tmp_path, children, dispatches, small_model, short_deadlines):
    small_model.release['title'].clear()
    agent = _root()
    _arm_chat(agent, history=[], session='chat-empty', message='ok')

    _trip(agent, tmp_path)

    assert dispatches[0]['handoff']['work'] == ht.FALLBACK_TITLE


def test_a_slow_known_call_does_not_hold_back_the_title_or_the_dispatch(
        tmp_path, children, dispatches, small_model, short_deadlines):
    small_model.release['known'].clear()
    agent = _root()
    _arm_chat(agent)

    _trip(agent, tmp_path)

    assert dispatches[0]['handoff']['work'] == 'adding Google Ads quote tracking', 'the title model answered'
    assert small_model.entered['known'].is_set()
    assert not small_model.release['known'].is_set(), 'dispatch returned with bullets still blocked'
    context = dispatches[0]['tasks'][0]['context']
    assert 'Known so far' not in context, 'the bullets had not landed yet'
    path = _path_in(context)
    small_model.release['known'].set()

    deadline = time.monotonic() + 6
    while 'Known so far' not in _read(path) and time.monotonic() < deadline:
        time.sleep(0.05)
    notes = _read(path)
    assert 'Known so far' in notes and 'The site has no Google Ads tag.' in notes, (
        'late bullets are written into the notes file the helper reads first')
    assert notes.index('Known so far') < notes.index("The user's request (verbatim)")


def test_bullets_that_are_in_time_ride_the_helper_context(
        tmp_path, children, dispatches, small_model, short_deadlines):
    # An immediately available script, not a sleep racing the grace window.
    assert small_model.release['known'].is_set()
    agent = _root()
    _arm_chat(agent)

    _trip(agent, tmp_path)

    assert 'The site has no Google Ads tag.' in dispatches[0]['tasks'][0]['context']


def test_the_two_calls_run_side_by_side_the_title_one_is_tiny(
        tmp_path, children, dispatches, small_model, short_deadlines):
    agent = _root()
    _arm_chat(agent)
    _trip(agent, tmp_path)
    kinds = [c['kind'] for c in small_model.calls if c['kind'] != 'other']
    assert sorted(kinds) == ['known', 'title']
    title = next(c for c in small_model.calls if c['kind'] == 'title')
    known = next(c for c in small_model.calls if c['kind'] == 'known')
    assert len(str(title['messages'])) < len(str(known['messages']))
    assert title['max_tokens'] == hn.TITLE_MAX_TOKENS
    assert title['task'] == known['task'] == 'delegation_router'


# ── the no-model chain, on the field's own words ───────────────────────────

class TestFallbackChain:
    def test_a_bare_go_ahead_answers_the_assistants_offer(self):
        out = ht.resolve(GO_AHEAD, last_assistant=OFFER)
        assert out[0] == 'starting on the tracking' and out.source == 'proposal'

    @pytest.mark.parametrize('msg', ['ok', 'yes', 'ye', 'Yes, do it.', 'yeah go ahead', 'sure, do it'])
    def test_go_aheads(self, msg):
        assert ht.is_go_ahead(msg)

    @pytest.mark.parametrize('msg', ['Why the fuck do you keep stopping', 'yes, compare the GPUs',
                                     'what about the banner?', 'Push it to the domain. dndsecurity.ca'])
    def test_other_messages_are_not_go_aheads(self, msg):
        assert not ht.is_go_ahead(msg)

    def test_a_complaint_never_becomes_the_offer(self):
        out = ht.resolve('Why the fuck do you keep stopping', last_assistant=OFFER)
        assert 'fuck' not in out[0].lower() and 'stopping' not in out[0].lower()

    def test_the_hand_offs_own_title_wins_over_an_older_request(self):
        out = ht.resolve(GO_AHEAD, last_title='fixing the card title timeout',
                         earlier=['Yes, do a full inspection.'])
        assert out[0] == 'fixing the card title timeout' and out.source == 'delegation'

    def test_the_placeholder_as_a_previous_title_is_not_carried_over(self):
        out = ht.resolve(GO_AHEAD, last_title=ht.FALLBACK_TITLE,
                         earlier=['Yes, do a full inspection.'])
        assert out[0] == 'doing a full inspection'

    def test_nothing_at_all_is_the_placeholder(self):
        out = ht.resolve('ok')
        assert out[0] == ht.FALLBACK_TITLE and out.source == 'default'

    def test_an_offer_that_names_no_known_action_is_not_used(self):
        assert ht.proposed_action('The weather is fine today.') == ('', '')

    def test_the_first_job_of_a_double_offer_names_the_title(self):
        title, _ = ht.proposed_action(
            'Want me to check the tracking and look at what people actually searched?')
        assert title == 'checking the tracking'


class TestAttachKnown:
    def test_the_section_goes_before_the_request_atomically_and_only_once(self, tmp_path):
        path = tmp_path / 'deleg_x.md'
        path.write_text("# Hand-off notes\n\nintro\n\n## The user's request (verbatim)\n\nask\n")
        assert hn.attach_known(path, ['one', 'two'])
        body = path.read_text()
        assert body.index('## Known so far') < body.index("## The user's request")
        assert '- one' in body and '- two' in body
        assert not hn.attach_known(path, ['three']), 'a second attach is a no-op'
        assert 'three' not in path.read_text()
        assert not list(tmp_path.glob('.known.*'))

    def test_a_missing_file_or_no_bullets_is_a_quiet_false(self, tmp_path):
        assert not hn.attach_known(tmp_path / 'nope.md', ['x'])
        (tmp_path / 'e.md').write_text('x')
        assert not hn.attach_known(tmp_path / 'e.md', [])

    def test_secrets_are_redacted_on_the_way_in(self, tmp_path):
        path = tmp_path / 'd.md'
        path.write_text("# n\n\n## The user's request (verbatim)\n\nask\n")
        hn.attach_known(path, ['key is OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx0123456789ABCDEF'])
        assert 'abcdefghijklmnopqrstuvwx0123456789' not in path.read_text()

"""Ownership, durable claim and clock identity of the verification episode (C1.5 B2).

Regression coverage distilled from the independent review probes: the once-only
claim under concurrency and schema migration, ownership by instance (never by
session or profile), a live human clock that is never replaced, and every
ledger failure failing closed.  Harness and fixtures are the real AIAgent loop,
the real durable ledger and a fake clock from the episode test module.
"""
from __future__ import annotations

import concurrent.futures
import sqlite3
import threading

import pytest

from agent import foreground_time_limit as fl
from tests.agent.test_foreground_verification_episode import (  # noqa: F401
    INTERNAL, _clean_async_state, _defaults, ad, clock, completion_turn, hand_off,
    make_agent, provider, stub_children, text,
)


def _consumed(did: str):
    conn = sqlite3.connect(ad._db_path())
    try:
        row = conn.execute(
            'SELECT verify_consumed_at FROM async_delegations WHERE delegation_id=?',
            (did,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def test_concurrent_claims_on_a_downgraded_ledger_grant_exactly_once(
        clock, provider, stub_children, monkeypatch):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    # A real, populated ledger on the immediately preceding schema.
    with ad._transaction() as conn:
        conn.execute('ALTER TABLE async_delegations DROP COLUMN verify_consumed_at')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(ad.claim_verification_allowance, [did] * 16))
    assert claims.count(True) == 1
    ad._reset_for_tests()
    assert not ad.claim_verification_allowance(did)
    assert _consumed(did) is not None

    def broken():
        raise sqlite3.OperationalError('injected ledger failure')

    monkeypatch.setattr(ad, '_connect', broken)
    assert not ad.claim_verification_allowance(did)


def test_concurrent_claims_mixing_dispatch_and_child_ids_win_once(
        provider, clock, stub_children):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def go(i: int) -> None:
        barrier.wait()
        wins.append(ad.claim_verification_allowance(
            ad.child_delegation_id(did, i) if i % 2 else did))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1 and len(wins) == 8


def test_competing_instances_claiming_one_id_get_one_window(
        clock, provider, stub_children):
    owner = make_agent(provider)
    did = hand_off(owner, provider)
    agents = [owner, make_agent(provider)]
    for agent in agents:  # both assert ownership: the durable claim decides
        fl.note_own_dispatch(agent, did)
    clock.advance(400)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda a: fl.begin_turn(a, INTERNAL, [did]), agents))
    assert sum(a._fg_episode.verification for a in agents) == 1


def test_another_instance_in_the_same_process_gets_nothing_and_burns_nothing(
        provider, clock, stub_children):
    a = make_agent(provider)
    did = hand_off(a, provider)
    b = make_agent(provider)  # another session's agent, same profile
    clock.advance(400)
    provider.queue[:] = [text('ok')]

    completion_turn(b, [did])

    episode = b._fg_episode
    assert not episode.verification and episode.finish_at is not None
    assert _consumed(did) is None
    provider.captured.clear()
    provider.queue[:] = [text('ok')]
    completion_turn(a, [did], task_id='c2')
    assert a._fg_episode.verification and a._fg_episode.limit == 120.0
    assert _consumed(did) is not None


def test_other_profile_ledger_means_no_window(
        clock, provider, stub_children, monkeypatch, tmp_path):
    owner = make_agent(provider)
    did = hand_off(owner, provider)
    other = tmp_path / 'other-profile'
    other.mkdir()
    monkeypatch.setattr(ad, 'get_clover_home', lambda: other)
    clock.advance(400)

    completion_turn(owner, [did])

    assert not owner._fg_episode.verification
    assert owner._fg_episode.expired()


def test_live_human_clock_is_inherited_and_the_window_comes_only_once_spent(
        provider, clock, stub_children):
    a = make_agent(provider)
    t0 = clock.t
    did = hand_off(a, provider)
    ep0 = a._fg_episode
    assert ep0.started_at == t0 and not ep0.verification
    clock.advance(100)
    provider.queue[:] = [text('noted')]
    completion_turn(a, [did])
    assert a._fg_episode is ep0 and not ep0.verification
    assert _consumed(did) is None, 'a live-clock delivery must not spend the allowance'

    clock.advance(250)  # 350 s after t0: spent
    provider.captured.clear()
    provider.queue[:] = [text('checking')]
    completion_turn(a, [ad.child_delegation_id(did, 0)], task_id='c2')
    ep2 = a._fg_episode
    assert ep2.verification and ep2.started_at == clock.t and ep2.limit == 120.0

    clock.advance(30)  # a human message mid-window gets its own full clock
    provider.captured.clear()
    provider.queue[:] = [text('hi')]
    a.run_conversation('hello', conversation_history=[], task_id='h2')
    ep3 = a._fg_episode
    assert not ep3.verification and ep3.limit == 300.0 and ep3.started_at == clock.t

    did2 = hand_off(a, provider, task_id='h3')  # a second dispatch under a live clock
    started = a._fg_episode.started_at
    clock.advance(50)
    provider.captured.clear()
    provider.queue[:] = [text('noted')]
    completion_turn(a, [did2], task_id='c3')
    assert a._fg_episode.started_at == started and not a._fg_episode.verification
    assert _consumed(did2) is None

    clock.advance(300)  # an internal non-completion turn after expiry: no window
    provider.captured.clear()
    provider.queue[:] = [text('x')]
    a.run_conversation('bg process done', conversation_history=[], task_id='i1',
                       persist_user_display_kind=INTERNAL)
    assert not a._fg_episode.verification and a._fg_episode.finish_at is not None
    assert _consumed(did2) is None


def test_ledger_errors_fail_closed(provider, clock, stub_children, monkeypatch):
    a = make_agent(provider)
    did = hand_off(a, provider)
    clock.advance(400)
    assert ad.claim_verification_allowance('never-dispatched') is False
    assert ad.claim_verification_allowance('') is False

    def boom(*_a, **_k):
        raise sqlite3.OperationalError('disk I/O error')

    monkeypatch.setattr(ad, '_transaction', boom)
    provider.queue[:] = [text('ok')]
    completion_turn(a, [did])
    episode = a._fg_episode
    assert not episode.verification and episode.finish_at is not None

"""Durable integration ownership in W1's profile store. No live I/O."""
from cron.presence import ThoughtStore


def test_budget_shared_across_contexts_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    a = ThoughtStore()
    assert hasattr(a, 'reserve_assessment'), 'missing shared reservation owner'
    for i in range(3):
        assert ThoughtStore().reserve_assessment(event_id=f'e{i}', thought_id=f't{i}', revision=1,
                                                day='2026-10-18', max_calls=3, tokens=512)
    assert not ThoughtStore().reserve_assessment(event_id='e3', thought_id='t3', revision=1,
                                                day='2026-10-18', max_calls=3, tokens=512)
    assert not a.reserve_assessment(event_id='e0', thought_id='t0', revision=1,
                                    day='2026-10-19', max_calls=3, tokens=512)


def test_owner_tables_are_bounded_and_resume_does_not_rearm_proposals(tmp_path, monkeypatch):
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    store = ThoughtStore()
    assert hasattr(store, 'resume'), 'missing explicit revocation reset'
    assert store.propose('old', 1, 'old-event', 0, {})
    store.stop()
    assert store.resume() == 2
    assert store.initiative_status() == (2, False)
    assert not store.claim('old', 1, 'new-owner', 2)
    for i in range(128):
        assert store.reserve_assessment(event_id=f'event{i}', thought_id=f'thought{i}', revision=1,
                                        day=str(i), max_calls=3, tokens=512)
    assert not store.reserve_assessment(event_id='overflow', thought_id='overflow', revision=1,
                                        day='next-day', max_calls=3, tokens=512)


def test_stop_epoch_and_unknown_send_are_never_replayed(tmp_path, monkeypatch):
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    a = ThoughtStore()
    assert hasattr(a, 'initiative_status'), 'missing profile revocation owner'
    assert a.initiative_status() == (0, False)
    assert a.propose('t', 1, 'e', 0, {'fingerprint': 'x'})
    assert a.claim('t', 1, 'worker', 0)
    assert not ThoughtStore().claim('t', 1, 'other', 0)
    assert a.attempt('t', 1, 'worker', 0)
    assert a.stop() == 1
    assert a.initiative_status() == (1, True)
    assert a.publications()[0]['state'] == 'unknown'
    assert not ThoughtStore().claim('t', 1, 'new', 1)
    assert not a.propose('t2', 1, 'e2', 0, {})

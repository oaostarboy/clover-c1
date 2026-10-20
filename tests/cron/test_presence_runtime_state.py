"""Live publication fences read one validated durable context state, never cache it."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent.presence_state import ContextKey, PresenceStoreError
from agent.workstreams import Scope
from cron.presence import ThoughtStore


def key(home, surface='telegram'):
    return ContextKey(Scope(str(home), '/project', 'private:' + surface, surface), surface + '-root')


def state(store, context):
    assert callable(getattr(store, 'runtime_state', None)), 'missing coherent runtime fence state'
    return store.runtime_state(context)


def test_absent_runtime_state_does_not_create_a_store(tmp_path):
    store = ThoughtStore()
    context = key(tmp_path)
    assert state(store, context) == (context, 0, (0, False))
    assert not store.path.exists()


def test_runtime_state_rechecks_generation_stop_and_resume_without_caching(tmp_path):
    store = ThoughtStore()
    context = key(tmp_path)
    generation = store.advance_generation()
    assert state(store, context) == (context, generation, (0, False))
    next_generation = store.advance_generation()
    assert state(store, context) == (context, next_generation, (0, False))
    epoch = store.stop()
    assert state(store, context) == (context, next_generation, (epoch, True))
    resumed = store.resume()
    assert state(store, context) == (context, next_generation, (resumed, False))


def test_runtime_state_rechecks_authenticated_link_and_foreign_context(tmp_path):
    store = ThoughtStore()
    telegram, desktop = key(tmp_path), key(tmp_path, 'desktop')
    now = datetime.now(timezone.utc)
    challenge = store.issue_link(telegram, 'telegram', now)
    before = state(store, telegram)
    assert before[0] == telegram
    assert store.accept_link(challenge, desktop, 'desktop', now)
    linked = store.linked_key(telegram)
    assert linked != telegram and linked == store.linked_key(desktop)
    assert state(store, telegram) == (linked, store.generation(), store.initiative_status())
    assert state(store, desktop) == state(store, telegram)
    foreign = replace(telegram, scope=replace(telegram.scope, project='/foreign'))
    assert state(store, foreign)[0] == foreign


def test_corrupt_runtime_state_refuses_without_repairing_bytes(tmp_path):
    store = ThoughtStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    damaged = b'corrupt presence database; preserve exactly'
    store.path.write_bytes(damaged)
    assert callable(getattr(store, 'runtime_state', None)), 'missing coherent runtime fence state'
    with pytest.raises(PresenceStoreError):
        store.runtime_state(key(tmp_path))
    assert store.path.read_bytes() == damaged


@pytest.mark.parametrize('version', [0, 1, 2, 3])
def test_legacy_runtime_state_is_read_only(tmp_path, version):
    import sqlite3
    store = ThoughtStore()
    context = key(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    if version:
        store.advance_generation()
    with sqlite3.connect(store.path) as conn:
        if version:
            for table in ('presence_links', 'presence_link_members', 'presence_link_challenges'):
                conn.execute('DROP TABLE ' + table)
            if version < 3:
                conn.execute('ALTER TABLE presence_epoch DROP COLUMN generation')
            if version < 2:
                for table in ('presence_epoch', 'presence_budget', 'presence_publications'):
                    conn.execute('DROP TABLE ' + table)
        conn.execute('PRAGMA user_version=' + str(version))
    before = store.path.read_bytes()
    assert state(store, context) == (context, 1 if version >= 3 else 0, (0, False))
    assert store.path.read_bytes() == before

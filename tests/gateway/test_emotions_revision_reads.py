"""Revision-read adequacy: post-read source mutations and benign accounting."""
import pytest
from tests.gateway.test_emotions_independent_review import host  # noqa: F401
from tests.gateway.test_emotions_final_fence import setup_command, mutate


@pytest.mark.parametrize('surface', ['telegram', 'desktop', 'desktop_slash'])
@pytest.mark.parametrize('boundary', ['_source', '_lineage'])
@pytest.mark.parametrize('race', ['policy', 'provider', 'managed', 'stop', 'generation', 'source', 'host', 'owner', 'sample'])
def test_mutation_in_final_revision_read_holds(host, monkeypatch, tmp_path, surface, boundary, race):
    from agent.emotions_snapshot import ReadFence
    policy = tmp_path / 'policy'; policy.mkdir()
    (policy / 'config.yaml').write_text('{}')
    monkeypatch.setenv('CLOVER_MANAGED_DIR', str(policy))
    agent, key, invoke = setup_command(host, monkeypatch, surface)
    real = getattr(ReadFence, boundary)
    count, trigger = 0, None
    def wrapped(*args, **kwargs):
        nonlocal count
        value = real(*args, **kwargs)
        count += 1
        if count == trigger:
            mutate(host, key, race)
        return value
    monkeypatch.setattr(ReadFence, boundary, staticmethod(wrapped))
    try:
        assert 'confidence=0.90' in invoke()
        trigger, count = count, 0
        assert trigger > 0
        assert 'confidence=' not in invoke()
        assert count == trigger
    finally:
        if agent:
            agent.close()


@pytest.mark.parametrize('surface', ['telegram', 'desktop', 'desktop_slash'])
def test_unrelated_accounting_write_does_not_revoke_private_reading(host, monkeypatch, surface):
    from agent import emotions
    import sqlite3
    agent, key, invoke = setup_command(host, monkeypatch, surface)
    real = emotions._peek_reading
    def read(*args, **kwargs):
        result = real(*args, **kwargs)
        with sqlite3.connect(host.db.db_path) as conn:
            conn.execute('UPDATE sessions SET title=? WHERE id=?', ('Synthetic bookkeeping', key.conversation_root))
        return result
    monkeypatch.setattr(emotions, '_peek_reading', read)
    try:
        assert 'confidence=0.90' in invoke()
    finally:
        if agent:
            agent.close()


@pytest.mark.parametrize('surface', ['telegram', 'desktop', 'desktop_slash'])
@pytest.mark.parametrize('race', ['stop', 'generation'])
def test_wal_control_change_during_final_source_read_holds(host, monkeypatch, surface, race):
    from agent.emotions_snapshot import ReadFence
    from cron.presence import ThoughtStore
    import sqlite3
    agent, key, invoke = setup_command(host, monkeypatch, surface)
    # Keep the WAL owner alive. Main-file mtime cannot detect these commits.
    conn = sqlite3.connect(ThoughtStore().path)
    assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    real = ReadFence._source
    calls = 0
    def source(*args):
        nonlocal calls
        value = real(*args)
        calls += 1
        if calls == 2:
            mutate(host, key, race)
        return value
    monkeypatch.setattr(ReadFence, '_source', staticmethod(source))
    try:
        assert 'confidence=' not in invoke()
        assert calls == 2
    finally:
        conn.close()
        if agent:
            agent.close()

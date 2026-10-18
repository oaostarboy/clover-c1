"""Real temporary SQLite persistence; no live config or source discovery."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
import sys

import pytest

from agent import presence_state as ps
from agent.workstreams import Scope

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path / "profile"))
    key = ps.ContextKey(Scope("default", "/project", "private:1", "1"), "conversation")
    ref = ps.EvidenceRef("session:s", "row:1", 1, sha256(b"synthetic source").hexdigest())
    sources = {ref: ps.EvidenceSnapshot(key, ref, True)}
    clock = [NOW]
    def reader(key, ref):
        return sources.get(ref)
    return SimpleNamespace(key=key, ref=ref, sources=sources, reader=reader,
                           clock=clock, home=tmp_path / "profile")


def make_state(h, *, enabled=True):
    assert hasattr(ps, "ThoughtState"), "missing durable continuity state"
    from cron.presence import ThoughtStore
    return ps.ThoughtState(h.key, h.reader, ThoughtStore(), clock=lambda: h.clock[0], enabled=enabled)


def test_restart_restores_thought_but_not_affect(host):
    state = make_state(host)
    thought = state.admit(host.ref, "Verify release packaging")
    assert thought is not None
    ticket = state.affect.begin(host.ref)
    assert state.affect.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    restored = make_state(host)
    assert restored.list_pending() == (thought,)
    assert restored.affect.latest is None
    assert restored.affect.posture == "ordinary"
    assert state.affect.posture == "careful_brief"


def test_disabled_and_empty_reads_create_no_database(host):
    state = make_state(host, enabled=False)
    assert state.admit(host.ref, "Do not persist") is None
    assert state.list_pending() == ()
    assert not host.home.exists()
    enabled = make_state(host)
    assert enabled.list_pending() == ()
    assert not host.home.exists()


def test_untrusted_source_is_not_durable(host):
    host.sources[host.ref] = replace(host.sources[host.ref], trusted=False)
    assert make_state(host).admit(host.ref, "Do not persist") is None
    assert not host.home.exists()


def test_thought_expiry_is_durable_and_does_not_replay(host):
    state = make_state(host)
    thought = state.admit(host.ref, "Verify release packaging", ttl=timedelta(seconds=5))
    host.clock[0] += timedelta(seconds=5)
    assert state.list_pending() == ()
    restored = make_state(host)
    assert restored.list_pending() == ()
    assert hasattr(restored, "inspect"), "missing dormant-record inspection"
    tombstone, = restored.inspect()
    assert tombstone.status == "expired"
    assert tombstone.summary == ""
    assert tombstone.revision == thought.revision + 1


@pytest.mark.parametrize("operation", ["corrected", "deleted"])
def test_source_changes_invalidate_thought_and_ephemeral_affect(host, operation):
    state = make_state(host)
    thought = state.admit(host.ref, "Old derived text")
    ticket = state.affect.begin(host.ref)
    assert state.affect.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    if operation == "deleted":
        del host.sources[host.ref]
    else:
        host.sources[host.ref] = replace(host.sources[host.ref], ref=replace(host.ref, revision=2))
    assert state.list_pending() == ()
    assert state.affect.posture == "ordinary"
    assert not state.affect.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    tombstone, = make_state(host).inspect()
    assert tombstone.status == "invalidated"
    assert tombstone.summary == ""
    assert tombstone.revision > thought.revision


def test_duplicate_source_revision_has_one_thought_and_ttl_is_not_refreshed(host):
    state = make_state(host)
    thought = state.admit(host.ref, "Original text")
    host.clock[0] += timedelta(seconds=1)
    assert state.admit(host.ref, "Duplicate or substituted text") == thought
    assert state.list_pending() == (thought,)


def test_explicit_correction_tombstones_all_source_derivatives_and_blocks_replay(host):
    state = make_state(host)
    first = state.admit(host.ref, "First text")
    state.admit(host.ref, "Second category", purpose="project_connection")
    ticket = state.affect.begin(host.ref)
    assert hasattr(state, "invalidate_source"), "missing correction control"
    assert state.invalidate_source(host.ref, status="corrected") == 2
    assert not state.affect.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    assert state.list_pending() == ()
    assert state.admit(host.ref, "Try to resurrect old source") is None
    assert all(t.status == "corrected" and t.summary == "" for t in state.inspect())
    assert state.invalidate_source(host.ref, status="corrected") == 0
    assert all(t.revision == first.revision + 1 for t in state.inspect())
    newer = replace(host.ref, revision=2, digest="0" * 64)
    host.sources[newer] = ps.EvidenceSnapshot(host.key, newer, True)
    assert state.admit(newer, "Corrected fresh text") is not None


@pytest.mark.parametrize("damage", ["newer", "corrupt", "malformed", "unknown_v0"])
def test_unsupported_or_corrupt_store_holds_without_repairing_it(host, damage):
    import sqlite3
    state = make_state(host)
    from cron.presence import ThoughtStore
    path = ThoughtStore().path
    path.parent.mkdir(parents=True, exist_ok=True)
    if damage == "corrupt":
        path.write_bytes(b"not a sqlite database")
    else:
        if damage == "malformed":
            assert state.admit(host.ref, "Seed")
        with sqlite3.connect(path) as conn:
            if damage == "newer":
                conn.execute("PRAGMA user_version=999")
                conn.execute("CREATE TABLE future_data (value TEXT)")
                conn.execute("INSERT INTO future_data VALUES ('keep')")
            elif damage == "malformed":
                conn.execute("UPDATE presence_thoughts SET record='{}'")
            else:
                conn.execute("CREATE TABLE alien_data (value TEXT)")
    before = path.read_bytes()
    assert state.list_pending() == ()
    assert state.admit(host.ref, "Must hold") is None
    assert state.last_error == "store_unavailable"
    assert path.read_bytes() == before


def test_correction_before_first_commit_blocks_late_source_replay(host):
    state = make_state(host)
    assert state.invalidate_source(host.ref, status="corrected") == 0
    assert state.admit(host.ref, "Late provider derivative") is None
    assert make_state(host).admit(host.ref, "After restart") is None


@pytest.mark.parametrize("field,value", [("summary", ""), ("summary", "界" * 700), ("summary", None), ("purpose", "diagnosis"), ("ttl", timedelta(0)), ("ttl", timedelta(days=8))])
def test_invalid_admission_is_rejected_before_disk(host, field, value):
    kwargs = {"summary": "Bounded thought", "ttl": timedelta(hours=1), "purpose": "follow_up"}
    kwargs[field] = value
    with pytest.raises(ValueError):
        make_state(host).admit(host.ref, **kwargs)
    assert not host.home.exists()


def test_concurrent_duplicate_admission_has_one_durable_identity(host):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    barrier = threading.Barrier(6)
    def admit(_):
        state = make_state(host)
        barrier.wait(timeout=10)
        return state.admit(host.ref, "Concurrent thought")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(admit, range(6)))
    assert all(results)
    assert len({t.thought_id for t in results}) == 1
    assert len(make_state(host).list_pending()) == 1


def test_profile_record_cap_is_atomic_and_never_evicts_tombstones(host, monkeypatch):
    from cron import presence as storage
    monkeypatch.setattr(storage, "MAX_RECORDS", 2, raising=False)
    state = make_state(host)
    first = state.admit(host.ref, "First")
    second_ref = replace(host.ref, source_id="row:2")
    host.sources[second_ref] = ps.EvidenceSnapshot(host.key, second_ref, True)
    assert state.admit(second_ref, "Second")
    assert state.invalidate_source(host.ref, status="deleted") == 1
    third_ref = replace(host.ref, source_id="row:3")
    host.sources[third_ref] = ps.EvidenceSnapshot(host.key, third_ref, True)
    assert state.admit(third_ref, "Over capacity") is None
    assert state.last_error == "store_unavailable"
    assert len(state.inspect()) == 2
    assert any(t.thought_id == first.thought_id and t.status == "deleted" for t in state.inspect())


def test_profile_byte_cap_rolls_back_admission(host, monkeypatch):
    from cron import presence as storage
    monkeypatch.setattr(storage, "MAX_STORE_BYTES", 100)
    state = make_state(host)
    assert state.admit(host.ref, "A bounded summary that exceeds a tiny test budget") is None
    assert state.inspect() == ()


def test_captured_thought_revision_is_not_current_after_correction(host):
    state = make_state(host)
    captured = state.admit(host.ref, "Old text")
    assert hasattr(state, "is_current"), "missing final-publication revalidation API"
    assert state.is_current(captured)
    make_state(host).invalidate_source(host.ref, status="corrected")
    assert not state.is_current(captured)


@pytest.mark.parametrize("field", ["profile", "project", "audience", "root", "conversation_root"])
def test_durable_thought_never_crosses_exact_context(host, field):
    state = make_state(host)
    thought = state.admit(host.ref, "Scoped thought")
    key = replace(host.key, conversation_root="foreign") if field == "conversation_root" else replace(host.key, scope=replace(host.key.scope, **{field: "foreign"}))
    from cron.presence import ThoughtStore
    foreign = ps.ThoughtState(key, host.reader, ThoughtStore(), clock=lambda: host.clock[0], enabled=True)
    assert foreign.list_pending() == ()
    assert not foreign.is_current(thought)


@pytest.mark.parametrize("change", ["naive_time", "infinite_ttl", "unknown_status", "empty_summary", "bad_revision", "foreign_payload"])
def test_semantically_corrupt_records_are_held(host, change):
    import json
    import sqlite3
    state = make_state(host)
    state.admit(host.ref, "Valid text")
    from cron.presence import ThoughtStore
    with sqlite3.connect(ThoughtStore().path) as conn:
        payload = json.loads(conn.execute("SELECT record FROM presence_thoughts").fetchone()[0])
        if change == "naive_time": payload["created_at"] = "2026-10-18T12:00:00"
        elif change == "infinite_ttl": payload["expires_at"] = "2099-10-18T12:00:00+00:00"
        elif change == "unknown_status": payload["status"] = "authorized"
        elif change == "empty_summary": payload["summary"] = ""
        elif change == "bad_revision": payload["revision"] = True
        else: payload["key"]["scope"]["root"] = "foreign"
        conn.execute("UPDATE presence_thoughts SET record=?", (json.dumps(payload),))
    assert state.list_pending() == ()
    assert state.last_error == "store_unavailable"


def test_new_source_revision_supersedes_older_derivatives_atomically(host):
    state = make_state(host)
    old = state.admit(host.ref, "Old version")
    newer = replace(host.ref, revision=2, digest="0" * 64)
    host.sources[newer] = ps.EvidenceSnapshot(host.key, newer, True)
    fresh = state.admit(newer, "Fresh version")
    assert fresh is not None
    assert state.list_pending() == (fresh,)
    assert not state.is_current(old)
    assert state.admit(host.ref, "Stale result") is None


def test_disabled_control_does_not_create_store_and_can_delete_existing_record(host):
    disabled = make_state(host, enabled=False)
    assert disabled.invalidate_source(host.ref, status="deleted") == 0
    assert not host.home.exists()
    active = make_state(host)
    active.admit(host.ref, "Delete after feature off")
    assert disabled.invalidate_source(host.ref, status="deleted") == 1
    assert disabled.inspect()[0].status == "deleted"


def test_shared_scope_never_admits_presence(host):
    shared_key = replace(host.key, scope=replace(host.key.scope, audience="shared:group:1"))
    reader = lambda key, ref: ps.EvidenceSnapshot(shared_key, ref, True)
    from cron.presence import ThoughtStore
    state = ps.ThoughtState(shared_key, reader, ThoughtStore(), clock=lambda: NOW, enabled=True)
    assert state.admit(host.ref, "Not a private source") is None
    assert state.affect.begin(host.ref) is None
    assert not host.home.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_new_presence_database_is_owner_only_even_in_existing_cron_directory(host):
    import stat
    (host.home / "cron").mkdir(parents=True, mode=0o755)
    assert make_state(host).admit(host.ref, "Private continuity")
    from cron.presence import ThoughtStore
    assert stat.S_IMODE(ThoughtStore().path.stat().st_mode) == 0o600


def test_disabling_state_revokes_affect_and_does_no_new_admission(host):
    state = make_state(host)
    assert hasattr(state, "set_enabled"), "missing combined state control"
    ticket = state.affect.begin(host.ref)
    state.set_enabled(False)
    assert not state.affect.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    assert state.admit(host.ref, "Disabled") is None
    assert state.list_pending() == ()
    assert not host.home.exists()


def test_correction_during_readback_never_returns_stale_thought(host):
    state = make_state(host)
    thought = state.admit(host.ref, "Captured text")
    def reader(key, ref):
        make_state(host).invalidate_source(ref, status="corrected")
        return host.sources[ref]
    state.reader = reader
    assert state.list_pending() == ()
    assert not state.is_current(thought)


def test_expiry_store_failure_holds_instead_of_raising(host, monkeypatch):
    state = make_state(host)
    state.admit(host.ref, "Soon expired", ttl=timedelta(seconds=1))
    host.clock[0] += timedelta(seconds=1)
    def fail(*args):
        raise ps.PresenceStoreError("simulated busy/corrupt write")
    monkeypatch.setattr(state.store, "invalidate", fail)
    assert state.list_pending() == ()
    assert state.last_error == "store_unavailable"


def test_profile_path_is_resolved_at_operation_time(host, monkeypatch, tmp_path):
    from cron.presence import ThoughtStore
    state = make_state(host)
    original = state.admit(host.ref, "Profile one")
    store = ThoughtStore()
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path / "other-profile"))
    assert store.list(host.key) == ()
    assert not (tmp_path / "other-profile").exists()
    monkeypatch.setenv("CLOVER_HOME", str(host.home))
    assert store.list(host.key) == (original,)


def test_empty_v0_database_migrates_and_never_contains_affect(host):
    import sqlite3
    from cron.presence import ThoughtStore
    store = ThoughtStore()
    store.path.parent.mkdir(parents=True)
    store.path.touch()
    state = make_state(host)
    assert state.admit(host.ref, "A thought, not an emotion history")
    assert state.affect.commit(state.affect.begin(host.ref), {"valence": -1, "arousal": 1, "confidence": 1})
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        record = conn.execute("SELECT record FROM presence_thoughts").fetchone()[0]
        assert all(name not in record for name in ("valence", "arousal", "confidence", "AffectReading"))


def test_recursively_corrupt_json_holds(host):
    import sqlite3
    from cron.presence import ThoughtStore
    state = make_state(host)
    state.admit(host.ref, "Seed")
    with sqlite3.connect(ThoughtStore().path) as conn:
        conn.execute("UPDATE presence_thoughts SET record=?", ("[" * 1500 + "0" + "]" * 1500,))
    assert state.list_pending() == ()
    assert state.last_error == "store_unavailable"


def test_duplicate_admission_after_expiry_does_not_return_or_refresh_thought(host):
    state = make_state(host)
    state.admit(host.ref, "Old thought", ttl=timedelta(seconds=1))
    host.clock[0] += timedelta(seconds=1)
    assert state.admit(host.ref, "Try to refresh", ttl=timedelta(hours=1)) is None
    assert state.list_pending() == ()
    assert state.inspect()[0].status == "expired"


def test_success_after_transient_store_failure_clears_error(host, monkeypatch):
    from cron import presence as storage
    state = make_state(host)
    monkeypatch.setattr(storage, "MAX_STORE_BYTES", 100)
    assert state.admit(host.ref, "Held") is None
    assert state.last_error == "store_unavailable"
    monkeypatch.setattr(storage, "MAX_STORE_BYTES", 64 * 1024)
    assert state.admit(host.ref, "Retry after storage recovery") is not None
    assert state.last_error is None


def test_correction_from_another_owner_revokes_existing_and_future_affect(host):
    state = make_state(host)
    state.admit(host.ref, "A pending thought")
    ticket = state.affect.begin(host.ref)
    assert state.affect.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    other = make_state(host)
    assert other.invalidate_source(host.ref, status="corrected") == 1
    # The original message may remain unchanged when the user corrects the
    # engine's reading rather than editing the source. Durable revocation wins.
    assert state.affect.latest is None
    assert state.affect.begin(host.ref) is None
    assert make_state(host).affect.begin(host.ref) is None

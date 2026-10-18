"""Synthetic affect only; no provider, prompt or delivery path is activated."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest

from agent import presence_state as ps
from agent.workstreams import Scope
from tests.agent.test_presence_shadow import host as ingress_host  # noqa: F401

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


@pytest.fixture
def host():
    key = ps.ContextKey(Scope("default", "/project", "private:1", "1"), "conversation")
    ref = ps.EvidenceRef("session:s", "row:1", 1, sha256(b"synthetic source").hexdigest())
    clock = [NOW]
    source = [ps.EvidenceSnapshot(key, ref, trusted=True)]
    state = ps.AffectState(key, lambda key, ref: source[0], clock=lambda: clock[0], enabled=True)
    return SimpleNamespace(key=key, ref=ref, clock=clock, source=source, state=state)


def test_synthetic_distress_is_tentative_posture_not_authority(host):
    ticket = host.state.begin(host.ref)
    assert ticket is not None
    assert host.state.commit(ticket, {"valence": -0.8, "arousal": 0.9, "confidence": 0.9})
    assert host.state.posture == "careful_brief"
    reading = host.state.latest
    assert reading.source == host.ref
    assert reading.observed_at == NOW
    assert not hasattr(reading, "authorized")


@pytest.mark.parametrize("age", [120, 121, -1])
def test_affect_expiry_is_strict_and_clock_rollback_is_neutral(host, age):
    ticket = host.state.begin(host.ref)
    assert host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    host.clock[0] += timedelta(seconds=age)
    assert host.state.latest is None
    assert host.state.posture == "ordinary"


def test_correction_clears_reading_and_rejects_inflight_result(host):
    ticket = host.state.begin(host.ref)
    assert host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    assert hasattr(host.state, "correct"), "missing correction invalidation"
    host.state.correct()
    assert host.state.posture == "ordinary"
    assert not host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})


def test_newer_turn_wins_over_delayed_assessment(host):
    old = host.state.begin(host.ref)
    fresh = host.state.begin(host.ref)
    assert host.state.commit(fresh, {"valence": 0, "arousal": 0, "confidence": 1})
    assert not host.state.commit(old, {"valence": -1, "arousal": 1, "confidence": 1})
    assert host.state.posture == "ordinary"


@pytest.mark.parametrize("change", ["deleted", "revision", "digest", "untrusted", "profile", "project", "audience", "root", "conversation_root"])
def test_owner_readback_rejects_changed_or_foreign_affect(host, change):
    ticket = host.state.begin(host.ref)
    assert host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    snap = host.source[0]
    if change == "deleted":
        host.source[0] = None
    elif change == "revision":
        host.source[0] = replace(snap, ref=replace(host.ref, revision=2))
    elif change == "digest":
        host.source[0] = replace(snap, ref=replace(host.ref, digest="0" * 64))
    elif change == "untrusted":
        host.source[0] = replace(snap, trusted=False)
    elif change == "conversation_root":
        host.source[0] = replace(snap, key=replace(host.key, conversation_root="foreign"))
    else:
        host.source[0] = replace(snap, key=replace(host.key, scope=replace(host.key.scope, **{change: "foreign"})))
    assert host.state.posture == "ordinary"
    assert not host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    assert host.state.begin(host.ref) is None


@pytest.mark.parametrize("field,value", [("valence", True), ("valence", float("nan")), ("arousal", float("inf")), ("confidence", -1), ("valence", "-1"), ("arousal", 2), ("valence", -2), ("confidence", None)])
def test_malformed_axes_are_rejected_without_stale_distress(host, field, value):
    ticket = host.state.begin(host.ref)
    sample = {"valence": -1, "arousal": 1, "confidence": 1}
    sample[field] = value
    assert not host.state.commit(ticket, sample)
    assert host.state.posture == "ordinary"


@pytest.mark.parametrize("sample", [{}, {"valence": -1}, None, [], {"valence": 0, "arousal": 0, "confidence": 1, "diagnosis": "ignored"}])
def test_partial_or_unknown_fields_are_not_assessments(host, sample):
    assert not host.state.commit(host.state.begin(host.ref), sample)
    assert host.state.latest is None


def test_disabled_does_not_read_evidence_and_revokes_tickets(host):
    assert hasattr(host.state, "set_enabled"), "missing revocation boundary"
    ticket = host.state.begin(host.ref)
    host.state.set_enabled(False)
    host.state.reader = lambda *args: pytest.fail("disabled evidence read")
    assert host.state.latest is None
    assert host.state.begin(host.ref) is None
    assert not host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    host.state.set_enabled(True)
    assert not host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})


def test_slow_assessment_cannot_extend_original_ttl(host):
    ticket = host.state.begin(host.ref)
    host.clock[0] += timedelta(seconds=120)
    assert not host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})


@pytest.mark.parametrize("field,value", [("owner", ""), ("source_id", ""), ("revision", True), ("revision", 0), ("digest", "not-a-digest"), ("digest", "A" * 64)])
def test_malformed_source_identifiers_are_rejected_without_normalization(host, field, value):
    with pytest.raises(ValueError):
        replace(host.ref, **{field: value})


@pytest.mark.parametrize("field", ["profile", "project", "audience", "root", "conversation_root"])
def test_incomplete_context_is_not_a_scope(host, field):
    with pytest.raises(ValueError):
        if field == "conversation_root":
            replace(host.key, conversation_root="")
        else:
            replace(host.key, scope=replace(host.key.scope, **{field: ""}))


def test_assessment_ticket_commits_only_once(host):
    ticket = host.state.begin(host.ref)
    assert host.state.commit(ticket, {"valence": 0, "arousal": 0, "confidence": 1})
    assert not host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    assert host.state.posture == "ordinary"


def test_correction_during_owner_readback_wins_over_commit(host):
    ticket = host.state.begin(host.ref)
    def reader(*args):
        host.state.correct()
        return host.source[0]
    host.state.reader = reader
    assert not host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": 1})
    assert host.state.latest is None


def test_invalid_ticket_is_rejected_without_raising(host):
    assert not host.state.commit(None, {"valence": -1, "arousal": 1, "confidence": 1})


@pytest.mark.parametrize("confidence,expected", [(0.69, "ordinary"), (0.7, "careful_brief")])
def test_confidence_threshold_is_only_a_posture_boundary(host, confidence, expected):
    ticket = host.state.begin(host.ref)
    assert host.state.commit(ticket, {"valence": -1, "arousal": 1, "confidence": confidence})
    assert host.state.posture == expected


@pytest.mark.parametrize("operation", ["correct", "delete"])
def test_real_receipt_backed_source_change_clears_durable_and_affect(ingress_host, operation):
    import sqlite3
    from agent import workstream_origin as origin
    from cron.presence import ThoughtStore
    from tests.agent.test_presence_shadow import prepare
    h = ingress_host
    turn = prepare(h, "Carry thought: verify package compatibility")
    key = ps.ContextKey(turn.scope, turn.conversation_root)
    ref = ps.EvidenceRef("session:" + turn.session_id, str(turn.row_id), 1, origin.text_digest(turn.query))
    def reader(requested_key, requested_ref):
        receipt = origin.read_human_origin(h.db, turn.session_id, turn.row_id)
        rows = h.db.get_messages_around(turn.session_id, turn.row_id, window=0)["window"]
        if requested_key != key or requested_ref != ref or not rows or not receipt or receipt.origin != "human" or receipt.profile != key.scope.profile:
            return None
        # Real owner readback, never a URL/role-shaped assertion. This callback
        # is a fixture adapter; production source-owner wiring remains W0's job.
        current = replace(ref, digest=origin.text_digest(rows[0]["content"]))
        return ps.EvidenceSnapshot(key, current, True)
    state = ps.ThoughtState(key, reader, ThoughtStore(), clock=lambda: h.clock[0], enabled=True)
    saved = state.admit(ref, "Verify package compatibility")
    assert saved is not None
    assert state.affect.commit(state.affect.begin(ref), {"valence": -1, "arousal": 1, "confidence": 1})
    with sqlite3.connect(h.home / "state.db") as conn:
        if operation == "correct":
            conn.execute("UPDATE messages SET content=? WHERE id=?", ("Corrected question", turn.row_id))
        else:
            conn.execute("DELETE FROM messages WHERE id=?", (turn.row_id,))
    restored = ps.ThoughtState(key, reader, ThoughtStore(), clock=lambda: h.clock[0], enabled=True)
    assert restored.list_pending() == ()
    assert state.affect.posture == "ordinary"
    assert not state.is_current(saved)


def test_correction_during_assessment_begin_prevents_provider_ticket(host):
    def reader(*args):
        host.state.correct()
        return host.source[0]
    host.state.reader = reader
    assert host.state.begin(host.ref) is None

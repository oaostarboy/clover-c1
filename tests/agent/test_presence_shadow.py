"""Real SessionDB + authenticated rows + existing prepare/present seam.

All affect samples are synthetic fixtures, not provider output. There are no
model calls in this module. Network guard makes leaks test failures.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from agent import workstream_origin as origin
from agent import workstream_suggestions as surface
from agent import workstreams as ws
from clover_state import SessionDB
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


class Event:
    internal = False


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    config = {"presence": {"enabled": True}, "proactive": {
        "enabled": True, "mode": "active", "suggestions": {
            "enabled": True, "preferred_timing": "after_reply"}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    db = SessionDB(db_path=home / "state.db")
    db.create_session("s", source="telegram")
    agent = SimpleNamespace(platform="telegram", _chat_type="dm", _chat_id="1", _user_id="1",
                            session_id="s", _session_db=db, _human_ingress_stop_generation=0)
    clock = [NOW]
    monkeypatch.setattr(surface, "_now", lambda: clock[0])
    h = SimpleNamespace(agent=agent, db=db, clock=clock, home=home, config=config)
    yield h
    db.close()


def prepare(h, text, *, current=None):
    current = current if current is not None else [True]
    event = Event()
    scope = ws.scope_for_agent(h.agent)
    assert origin.mint_human_ingress(event, platform="telegram", scope=scope, original_text=text)
    ingress = origin.redeem_human_ingress(
        event, platform="telegram", profile=scope.profile, session_id="s", session_key="k",
        run_generation=1, is_current=lambda: current[0])
    h.agent._trusted_human_ingress = ingress
    ingress = origin.bind_human_ingress_to_turn(ingress, h.agent, text)
    assert ingress is not None
    row = {"role": "user", "content": text, "human_ingress": ingress}
    h.db.append_messages_batch("s", [row])
    surface.prepare_offer_turn(h.agent, row)
    return h.agent._workstream_suggestion_turn


def present(h, *, current=lambda: True):
    result = {"final_response": "ordinary reply"}
    assert surface.present_offer(h.agent, result, is_current=current) is None
    assert result == {"final_response": "ordinary reply"}
    return getattr(h.agent, "_presence_shadow", None)


def test_thought_expiry_holds_without_model_or_send(host):
    prepare(host, "Carry thought: deployment packaging verification")
    host.clock[0] += timedelta(hours=1)
    prepare(host, "deployment packaging verification")
    proposal = present(host)
    assert proposal is not None
    assert proposal.action == "hold" and proposal.reason == "expired"


def test_synthetic_negative_affect_shapes_only_shadow_posture(host):
    from agent import presence_shadow as presence
    turn = prepare(host, "Carry thought: deployment packaging verification")
    assert hasattr(presence, "admit_affect"), "missing source-bound affect admission"
    assert presence.admit_affect(host.agent, turn, {
        "valence": -0.8, "arousal": 0.9, "confidence": 0.9}, now=host.clock[0])
    prepare(host, "deployment packaging verification")
    proposal = present(host)
    assert proposal is not None
    assert proposal.posture == "careful_brief" and proposal.surface is False
    from cron import notepad as np
    assert np.list_receipts(ws.scope_for_agent(host.agent), kinds=["authorized", "delivered", "executed"]) == []


def test_affect_expires_before_pending_thought(host):
    from agent import presence_shadow as presence
    turn = prepare(host, "Carry thought: deployment packaging verification")
    assert presence.admit_affect(host.agent, turn, {
        "valence": -1, "arousal": 1, "confidence": 1}, now=host.clock[0])
    host.clock[0] += timedelta(seconds=120)
    prepare(host, "deployment packaging verification")
    proposal = present(host)
    assert proposal is not None
    assert proposal.action == "offer" and proposal.posture == "ordinary"


@pytest.mark.parametrize("field", ["profile", "project", "audience", "root", "conversation_root"])
def test_foreign_pending_thought_never_crosses_scope(host, field):
    from dataclasses import replace
    prepare(host, "Carry thought: deployment packaging verification")
    thought = host.db._presence_pending
    if field == "conversation_root":
        foreign = replace(thought, conversation_root="other-root")
    else:
        foreign = replace(thought, scope=replace(thought.scope, **{field: "foreign"}))
    host.db._presence_pending = foreign
    prepare(host, "deployment packaging verification")
    proposal = present(host)
    assert proposal is None, "foreign thought leaked into this turn"


@pytest.mark.parametrize("operation", ["correct", "delete"])
def test_source_correction_or_delete_invalidates_derived_thought(host, operation):
    import sqlite3
    from agent import presence_shadow as presence
    turn = prepare(host, "Carry thought: deployment packaging verification")
    assert presence.admit_affect(host.agent, turn, {
        "valence": -1, "arousal": 1, "confidence": 1}, now=host.clock[0])
    prepare(host, "deployment packaging verification")
    # Simulate the source owner's correction/delete in the real temp database.
    with sqlite3.connect(host.home / "state.db") as conn:
        if operation == "correct":
            conn.execute("UPDATE messages SET content=? WHERE id=?", ("Corrected thought", turn.row_id))
        else:
            conn.execute("DELETE FROM messages WHERE id=?", (turn.row_id,))
    proposal = present(host)
    assert proposal is not None
    assert proposal.action == "hold" and proposal.reason == "source_changed"
    assert proposal.posture == "ordinary"
    assert host.db._presence_pending is None


def test_one_shadow_offer_per_thought_including_duplicate_events(host):
    prepare(host, "Carry thought: deployment packaging verification")
    turn = prepare(host, "deployment packaging verification")
    assert present(host).action == "offer"
    # Replay the same captured event, then a new event about the same thought.
    host.agent._workstream_suggestion_turn = turn
    assert present(host).action == "hold"
    prepare(host, "deployment packaging verification")
    assert present(host).reason == "duplicate"


@pytest.mark.parametrize("cancel", ["new_input", "Stop"])
def test_generation_cancellation_after_prepare_rejects_shadow(host, cancel):
    prepare(host, "Carry thought: deployment packaging verification")
    current = [True]
    prepare(host, "deployment packaging verification", current=current)
    if cancel == "new_input":
        current[0] = False
    else:
        host.agent._human_ingress_stop_generation += 1
    assert present(host) is None


def test_cancellation_during_source_read_does_not_commit_a_proposal(host, monkeypatch):
    prepare(host, "Carry thought: deployment packaging verification")
    prepare(host, "deployment packaging verification")
    current = [True]
    real = host.db.get_messages_around

    def read_then_cancel(*args, **kwargs):
        value = real(*args, **kwargs)
        current[0] = False
        return value

    monkeypatch.setattr(host.db, "get_messages_around", read_then_cancel)
    assert present(host, current=lambda: current[0]) is None
    assert host.db._presence_pending.offered_event_id == ""


def test_existing_session_cap_can_only_hold_presence_even_with_distress(host):
    from agent import presence_shadow as presence
    turn = prepare(host, "Carry thought: deployment packaging verification")
    assert presence.admit_affect(host.agent, turn, {
        "valence": -1, "arousal": 1, "confidence": 1}, now=host.clock[0])
    host.config["proactive"]["suggestions"]["max_per_session"] = 0
    (host.home / "config.yaml").write_text(yaml.safe_dump(host.config), encoding="utf-8")
    prepare(host, "deployment packaging verification")
    proposal = present(host)
    assert proposal is not None
    assert proposal.action == "hold" and proposal.reason == "session_cap"


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -float("inf"), "junk", None])
def test_invalid_synthetic_reading_cannot_shape_posture(host, bad):
    from agent import presence_shadow as presence
    turn = prepare(host, "Carry thought: deployment packaging verification")
    assert presence.admit_affect(host.agent, turn, {
        "valence": -0.8, "arousal": 0.9, "confidence": bad}, now=host.clock[0])
    prepare(host, "deployment packaging verification")
    assert present(host).posture == "ordinary"


def test_presence_revocation_clears_prior_shadow_without_new_presence_work(host, monkeypatch):
    from agent import presence_shadow as presence
    prepare(host, "Carry thought: deployment packaging verification")
    prepare(host, "deployment packaging verification")
    assert present(host).action == "offer"
    host.config["presence"]["enabled"] = False
    (host.home / "config.yaml").write_text(yaml.safe_dump(host.config), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("disabled presence performed work")

    monkeypatch.setattr(presence, "prepare_shadow", forbidden)
    monkeypatch.setattr(presence, "evaluate_shadow", forbidden)
    prepare(host, "deployment packaging verification")
    assert present(host) is None


@pytest.mark.parametrize("change", ["scope", "query", "row", "cancel", "disabled"])
def test_affect_admission_rejects_foreign_or_stale_source(host, change):
    from dataclasses import replace
    from agent import presence_shadow as presence
    current = [True]
    turn = prepare(host, "Carry thought: deployment packaging verification", current=current)
    if change == "scope":
        turn = replace(turn, scope=replace(turn.scope, project="foreign"))
    elif change == "query":
        turn = replace(turn, query="different source")
    elif change == "row":
        turn = replace(turn, row_id=turn.row_id + 1)
    elif change == "cancel":
        current[0] = False
    else:
        host.config["presence"]["enabled"] = False
        (host.home / "config.yaml").write_text(yaml.safe_dump(host.config), encoding="utf-8")
    assert not presence.admit_affect(host.agent, turn, {
        "valence": -1, "arousal": 1, "confidence": 1}, now=host.clock[0])


def test_duplicate_admission_does_not_reset_expiry_or_rearm_offer(host):
    from agent import presence_shadow as presence
    source = prepare(host, "Carry thought: deployment packaging verification")
    prepare(host, "deployment packaging verification")
    assert present(host).action == "offer"
    before = host.db._presence_pending
    presence.prepare_shadow(host.agent, source, now=host.clock[0] + timedelta(minutes=20))
    assert host.db._presence_pending == before


# Characterization coverage for the inherited policy and no-provider contract.
@pytest.mark.parametrize("timing,reason", [(None, "no_timing_preference"), ("never", "timing")])
def test_negative_affect_cannot_upgrade_disallowed_timing(host, timing, reason):
    from agent import presence_shadow as presence
    turn = prepare(host, "Carry thought: deployment packaging verification")
    assert presence.admit_affect(host.agent, turn, {
        "valence": -1, "arousal": 1, "confidence": 1}, now=host.clock[0])
    host.config["proactive"]["suggestions"]["preferred_timing"] = timing
    (host.home / "config.yaml").write_text(yaml.safe_dump(host.config), encoding="utf-8")
    prepare(host, "deployment packaging verification")
    proposal = present(host)
    assert proposal is not None
    assert proposal.action == "hold" and proposal.reason == reason
    assert proposal.surface is False


def test_irrelevant_event_holds_with_no_inference(host):
    prepare(host, "Carry thought: deployment packaging verification")
    prepare(host, "garden roses")
    proposal = present(host)
    assert proposal is not None
    assert proposal.action == "hold" and proposal.reason == "irrelevant"
    assert not host.db._presence_pending.offered_event_id


def test_fresh_disabled_flag_never_invokes_presence_or_creates_state(host, monkeypatch):
    from agent import presence_shadow as presence
    host.config.pop("presence")
    (host.home / "config.yaml").write_text(yaml.safe_dump(host.config), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("presence ran while default off")

    monkeypatch.setattr(presence, "prepare_shadow", forbidden)
    monkeypatch.setattr(presence, "evaluate_shadow", forbidden)
    prepare(host, "Carry thought: deployment packaging verification")
    assert present(host) is None
    assert not hasattr(host.db, "_presence_pending")
    assert not hasattr(host.agent, "_presence_shadow")


def test_synthetic_user_role_without_origin_cannot_admit_thought(host):
    row = {"role": "user", "content": "Carry thought: deployment packaging verification"}
    host.db.append_messages_batch("s", [row])
    surface.prepare_offer_turn(host.agent, row)
    assert not hasattr(host.db, "_presence_pending")


def test_corrected_synthetic_affect_and_numeric_bounds(host):
    from agent import presence_shadow as presence
    turn = prepare(host, "Carry thought: deployment packaging verification")
    assert presence.admit_affect(host.agent, turn, {
        "valence": -99, "arousal": 99, "confidence": 99}, now=host.clock[0])
    reading = host.db._presence_pending.reading
    assert (reading.valence, reading.arousal, reading.confidence) == (-1, 1, 1)
    assert presence.admit_affect(host.agent, turn, {}, now=host.clock[0])
    prepare(host, "deployment packaging verification")
    assert present(host).posture == "ordinary"


def test_reopening_database_does_not_restore_ephemeral_thought_or_affect(host):
    prepare(host, "Carry thought: deployment packaging verification")
    fresh = SessionDB(db_path=host.home / "state.db")
    try:
        assert not hasattr(fresh, "_presence_pending")
    finally:
        fresh.close()

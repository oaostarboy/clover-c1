"""Host consumers of the trusted human-origin receipt, through the real path.

Real Telegram adapter -> GatewayRunner -> AIAgent.run_conversation -> SessionDB
row + receipt -> workstream human consumer -> scoped notepad record -> the next
ordinary turn's current-turn sidecar.  Only the provider/network edge is
scripted (see ``Harness`` in test_trusted_human_origin_ingress).  Nothing here
installs a reader, plants a row, or calls a consumer helper directly.
"""

import sqlite3

import pytest

from agent import workstreams
from cron import notepad
from tests.gateway.test_trusted_human_origin_ingress import (  # noqa: F401
    Harness,
    network_guard,
)

SETTINGS = {
    "enabled": True,
    "recall": {"enabled": True},
    "suggestions": {"enabled": True},
}


def _scope(h, *, user_id="12345"):
    return workstreams.scope_for_surface(
        platform="telegram", chat_type="dm", chat_id="12345", user_id=user_id,
        project=str(h.project.resolve()),
    )


def _user_content(request):
    return [m for m in request["messages"] if m.get("role") == "user"][-1]["content"]


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=SETTINGS)
    yield h
    h.close()


def test_correction_from_real_human_turn_is_recalled_on_the_next_turn(enabled):
    enabled.inbound_human("Correction: the deployment target is staging-2", message_id=100)

    rows = notepad.list_workstreams(_scope(enabled), kinds=["correction"])
    assert len(rows) == 1
    record = rows[0]
    assert "staging-2" in record["summary"]
    assert record["approved"] and record["status"] == "open"
    assert record["source"].startswith("human:")
    assert record["session_id"] == enabled.session_id()

    # The receipt was consumed after the effect committed.
    first_row = enabled.user_rows()[0]
    assert enabled.db.get_human_origin(enabled.session_id(), first_row["id"]) is None
    with sqlite3.connect(enabled.db_path) as conn:
        assert conn.execute(
            "SELECT consumed_by FROM human_origin_receipts").fetchall() == [
                ("workstream_human_hooks",)]
    # Turn 1 request carried no sidecar of its own correction.
    assert "[ws:" not in _user_content(enabled.scripted.requests[0])

    enabled.inbound_human("Which deployment target is configured?", message_id=101)
    api_text = _user_content(enabled.scripted.requests[1])
    assert api_text.startswith("Which deployment target is configured?")
    assert "staging-2" in api_text
    assert f"[ws:{record['workstream_id']}@r1]" in api_text
    # Persisted api_content equals the serialized provider bytes.
    stored = enabled.user_rows()[1]
    assert stored["content"] == "Which deployment target is configured?"
    assert stored.get("api_content") == api_text


# ── helpers ────────────────────────────────────────────────────────────────


@pytest.fixture
def agents(monkeypatch):
    """Observe (wrap, never replace) the real agent run to read its result."""
    from run_agent import AIAgent

    seen = []
    real = AIAgent.run_conversation

    def _observe(self, *args, **kwargs):
        seen.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(AIAgent, "run_conversation", _observe)
    return seen


def _all(h, kind=None):
    kinds = [kind] if kind else None
    return notepad.list_workstreams(_scope(h), kinds=kinds)


def _feedback(h, ws_id):
    return notepad.list_feedback(_scope(h), ws_id)


# ── several operations in one event ────────────────────────────────────────


def test_one_event_applies_several_explicit_operations_then_consumes_once(enabled):
    enabled.inbound_human(
        "Correction: the deploy window is Tuesday\n"
        "Correction: the rollback owner is dana", message_id=200)
    first = {r["summary"]: r["workstream_id"] for r in _all(enabled, "correction")}
    assert set(first) == {"the deploy window is Tuesday", "the rollback owner is dana"}
    a, b = first["the deploy window is Tuesday"], first["the rollback owner is dana"]

    enabled.inbound_human(
        f"Correction: {a}: the deploy window is Wednesday\n"
        f"Feedback: {b} accept: yes this one is right", message_id=201)

    live = {r["workstream_id"]: r for r in _all(enabled, "correction")
            if r["status"] == "open"}
    assert any("Wednesday" in r["summary"] for r in live.values())
    assert notepad.get_workstream(_scope(enabled), a)["status"] == "superseded"
    fb = _feedback(enabled, b)
    assert [f["kind"] for f in fb] == ["accept"] and fb[0]["actor"] == "user"
    # One consume per human event, after both effects.
    with sqlite3.connect(enabled.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM human_origin_receipts WHERE consumed_at IS NOT NULL"
        ).fetchone()[0] == 2


# ── fences ─────────────────────────────────────────────────────────────────


def test_stale_generation_persists_the_row_but_no_correction(enabled):
    from agent.workstream_origin import mint_human_ingress
    from gateway.platforms.base import MessageEvent
    from tests.gateway.test_trusted_human_origin_ingress import _user_source

    source = _user_source()
    event = MessageEvent(text="Correction: the deploy window is Tuesday",
                         source=source, message_id="m-1")
    assert mint_human_ingress(event, platform="telegram")
    stale = enabled.runner._begin_session_run_generation(
        "agent:main:telegram:dm:12345")
    enabled.runner._invalidate_session_run_generation(
        "agent:main:telegram:dm:12345", reason="stop")
    enabled.dispatch_with_generation(event, stale)
    assert len(enabled.user_rows()) == 1
    assert _all(enabled) == []


def test_stop_between_row_persist_and_consumer_blocks_the_effect(enabled, monkeypatch):
    """The receipt is already durable; Stop lands before the host consumes it."""
    from run_agent import AIAgent

    real = AIAgent._persist_session
    key = "agent:main:telegram:dm:12345"

    def _persist_then_stop(self, *args, **kwargs):
        out = real(self, *args, **kwargs)
        enabled.runner._invalidate_session_run_generation(key, reason="stop")
        return out

    monkeypatch.setattr(AIAgent, "_persist_session", _persist_then_stop)
    enabled.inbound_human("Correction: the deploy window is Tuesday")
    assert _all(enabled) == []
    row = enabled.user_rows()[0]
    # Provenance stays history: unconsumed receipt, no effect, no authority.
    assert enabled.db.get_human_origin(enabled.session_id(), row["id"]) is not None


# ── no provenance, no effect ───────────────────────────────────────────────


def test_unmarked_prose_and_wrapped_markers_change_nothing(enabled):
    enabled.inbound_human("I think the deployment target is staging-9", message_id=300)
    enabled.inbound_human("Please note: Correction: the target is prod", message_id=301)
    assert _all(enabled) == []


def test_secret_in_correction_is_redacted_before_it_is_durable(enabled):
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    enabled.inbound_human(f"Correction: deploy with token {secret} today", message_id=310)
    rows = _all(enabled, "correction")
    assert len(rows) == 1
    assert secret not in rows[0]["summary"] and secret not in rows[0]["title"]


def test_unknown_target_is_reported_denied_and_not_consumed(enabled, agents):
    enabled.inbound_human(
        "Correction: ws_0123456789abcdef: nothing to supersede here", message_id=320)
    assert _all(enabled) == []
    result = agents[-1]._human_workstream_result
    assert result.handled is False
    assert result.operations[0].status == "denied"
    assert "unknown workstream" in result.response_text
    row = enabled.user_rows()[0]
    assert enabled.db.get_human_origin(enabled.session_id(), row["id"]) is not None


# ── scope negatives and disabled mode ──────────────────────────────────────


def test_other_user_project_and_group_scopes_never_see_the_correction(enabled, tmp_path):
    enabled.inbound_human("Correction: the deployment target is staging-2", message_id=330)
    record = _all(enabled, "correction")[0]
    settings = workstreams.load_settings(SETTINGS_ROOT)
    query = "which deployment target is configured"
    assert record["workstream_id"] in workstreams.current_turn_note(
        _scope(enabled), query, settings)
    foreign = [
        _scope(enabled, user_id="999"),
        workstreams.scope_for_surface(
            platform="telegram", chat_type="dm", chat_id="12345", user_id="12345",
            project=str(tmp_path / "another-project")),
        workstreams.scope_for_surface(
            platform="telegram", chat_type="group", chat_id="-100", user_id="12345",
            project=str(enabled.project.resolve())),
        workstreams.make_scope(str(enabled.project.resolve()), audience="private:12345",
                               root="12345", profile=str(tmp_path / "other-profile")),
    ]
    for scope in foreign:
        assert workstreams.current_turn_note(scope, query, settings) == ""


SETTINGS_ROOT = {"proactive": SETTINGS}


def test_disabled_mode_has_no_notepad_receipts_or_extra_bytes(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=False)
    try:
        h.inbound_human("Correction: the deployment target is staging-2", message_id=340)
        h.inbound_human("Which deployment target is configured?", message_id=341)
        assert not (h.home / "cron" / "notepad.db").exists()
        assert "human_origin_receipts" not in h.tables()
        assert _user_content(h.scripted.requests[1]) == "Which deployment target is configured?"
        # (turn 1 may carry the once-per-install onboarding sidecar; turn 2
        # must carry nothing)
        assert not h.user_rows()[1].get("api_content")
    finally:
        h.close()

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


# ── bounded evidence for a real evidence-gap question ──────────────────────


def _seed_cron(h, monkeypatch, *, prompt, workdir=None, success=True):
    """A job and an execution in the harness profile's real cron owners."""
    import cron.executions as executions
    import cron.jobs as jobs

    cron_dir = h.home / "cron"
    (cron_dir / "output").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(jobs, "CLOVER_DIR", h.home)
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", cron_dir / "executions.db")
    job = jobs.create_job(prompt, "every 1h", name="nightly",
                          workdir=str(workdir or h.project))
    run = executions.create_execution(job["id"], source="test")
    executions.finish_execution(run["id"], success=success)
    return job["id"], run["id"]


def test_evidence_gap_question_gets_cited_local_evidence(enabled, monkeypatch):
    job_id, run_id = _seed_cron(
        enabled, monkeypatch, prompt="nightly reports database backup")
    enabled.inbound_human("Did the nightly reports backup run?", message_id=400)

    api_text = _user_content(enabled.scripted.requests[0])
    assert api_text.startswith("Did the nightly reports backup run?")
    assert f"[cron-execution:{job_id}:{run_id}]" in api_text
    assert "does not establish the requested deliverable outcome" in api_text
    assert enabled.user_rows()[0]["content"] == "Did the nightly reports backup run?"
    assert enabled.user_rows()[0].get("api_content", "").endswith(api_text.split("\n\n", 1)[1])


def test_evidence_gap_without_a_local_record_is_explicitly_unknown(enabled, monkeypatch):
    _seed_cron(enabled, monkeypatch, prompt="nightly reports database backup")
    enabled.inbound_human("Did the quarterly audit export finish?", message_id=410)
    api_text = _user_content(enabled.scripted.requests[0])
    assert "Evidence unknown: no matching, scope-verified local record was found." in api_text
    assert "cron-execution" not in api_text


def test_evidence_from_another_project_is_not_exposed(enabled, monkeypatch, tmp_path):
    other = tmp_path / "somewhere-else"
    other.mkdir()
    _seed_cron(enabled, monkeypatch, prompt="nightly reports database backup", workdir=other)
    enabled.inbound_human("Did the nightly reports backup run?", message_id=420)
    api_text = _user_content(enabled.scripted.requests[0])
    assert "cron-execution" not in api_text
    assert "Evidence unknown" in api_text


def test_ordinary_turns_get_no_evidence_bytes(enabled, monkeypatch):
    _seed_cron(enabled, monkeypatch, prompt="nightly reports database backup")
    enabled.inbound_human("Thanks, that looks good to me", message_id=430)
    enabled.inbound_human("Which reports database is configured?", message_id=431)
    for request in enabled.scripted.requests:
        content = _user_content(request)
        assert "cron-execution" not in content and "Evidence" not in content


def test_evidence_is_off_when_recall_is_not_enabled(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive={"enabled": True})
    try:
        _seed_cron(h, monkeypatch, prompt="nightly reports database backup")
        h.inbound_human("Did the nightly reports backup run?", message_id=440)
        assert _user_content(h.scripted.requests[0]) == "Did the nightly reports backup run?"
    finally:
        h.close()


# ── explicit timing / novelty feedback ─────────────────────────────────────


def _suggestion(h, wid):
    return workstreams.evaluate_suggestion(
        workstreams.Candidate(workstream_id=wid, text="review the rollback owner",
                              relevance=1.0, unsolicited=False),
        _scope(h), workstreams.load_settings(SETTINGS_ROOT),
    )


def test_explicit_feedback_drives_cooldown_and_stop_controls(enabled):
    enabled.inbound_human("Correction: the rollback owner is dana", message_id=500)
    wid = _all(enabled, "correction")[0]["workstream_id"]
    assert workstreams.feedback_state(_scope(enabled), wid) == "unknown"  # silence

    enabled.inbound_human(f"Feedback: {wid} defer: not this week", message_id=501)
    (deferred,) = _feedback(enabled, wid)
    assert deferred["kind"] == "defer" and deferred["defer_until"]
    assert deferred["note"] == "not this week"
    decision = _suggestion(enabled, wid)
    assert (decision.action, decision.reason) == ("defer", "cooldown")

    enabled.inbound_human(f"Feedback: {wid} stop", message_id=502)
    assert [f["kind"] for f in _feedback(enabled, wid)] == ["defer", "stop"]
    decision = _suggestion(enabled, wid)
    assert (decision.action, decision.reason) == ("suppress", "stopped")


def test_feedback_for_unknown_or_foreign_workstream_is_denied(enabled, agents):
    enabled.inbound_human("Feedback: ws_0123456789abcdef accept", message_id=510)
    result = agents[-1]._human_workstream_result
    assert result.handled is False
    assert result.operations[0].status == "denied"


def test_feedback_is_off_when_suggestions_are_not_enabled(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path,
                proactive={"enabled": True, "recall": {"enabled": True}})
    try:
        h.inbound_human("Correction: the rollback owner is dana", message_id=520)
        wid = notepad.list_workstreams(_scope(h), kinds=["correction"])[0]["workstream_id"]
        h.inbound_human(f"Feedback: {wid} reject", message_id=521)
        assert notepad.list_feedback(_scope(h), wid) == []
    finally:
        h.close()


# ── all-or-nothing within one event ────────────────────────────────────────


def test_failure_in_a_later_operation_leaves_no_partial_effect(enabled, agents, monkeypatch):
    enabled.inbound_human("Correction: the rollback owner is dana", message_id=600)
    wid = _all(enabled, "correction")[0]["workstream_id"]
    before = [r["workstream_id"] for r in _all(enabled)]

    real = notepad._new_id

    def _fail_feedback_id(prefix):
        if prefix == "fb":
            raise RuntimeError("simulated crash while writing feedback")
        return real(prefix)

    monkeypatch.setattr(notepad, "_new_id", _fail_feedback_id)
    enabled.inbound_human(
        "Correction: the deploy window is Tuesday\n"
        f"Feedback: {wid} accept", message_id=601)
    monkeypatch.setattr(notepad, "_new_id", real)

    assert [r["workstream_id"] for r in _all(enabled)] == before  # nothing partial
    assert _feedback(enabled, wid) == []
    result = agents[-1]._human_workstream_result
    assert result.handled is False and not result.consumed
    assert {o.status for o in result.operations} == {"error"}
    row = enabled.user_rows()[1]
    # Still unconsumed: no success was reported or spent before the commit.
    assert enabled.db.get_human_origin(enabled.session_id(), row["id"]) is not None

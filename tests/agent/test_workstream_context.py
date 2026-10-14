"""Workstream context must share Clover's current-turn API-content composer."""

from agent.turn_context import compose_user_api_content
from agent.memory_manager import current_workstream_context
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    _limit_on,
    make_agent,
    provider,
    text,
)


def test_workstream_context_is_appended_only_to_the_current_turn_api_copy():
    clean = "What did we decide about rollout?"
    note = "[ws:w-1@r2] Keep rollout private."

    composed = compose_user_api_content(
        clean,
        ext_prefetch_cache="",
        plugin_user_context="",
        work_plan="",
        workstream_context=note,
    )

    assert composed == clean + "\n\n" + note
    assert clean == "What did we decide about rollout?"


def test_empty_workstream_context_preserves_existing_byte_identity():
    content = "Same bytes, please."

    assert compose_user_api_content(
        content,
        ext_prefetch_cache="",
        plugin_user_context="",
        work_plan="",
        workstream_context="",
    ) is None


def test_local_recall_uses_profile_project_scope_and_runs_synchronously(
    monkeypatch, tmp_path
):
    import threading

    from agent import workstreams
    from clover_constants import get_clover_home

    observed = {}
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr(
        workstreams,
        "load_settings",
        lambda: type("S", (), {"enabled": True})(),
        raising=False,
    )

    def select(scope, query, settings):
        observed.update(scope=scope, query=query, thread=threading.current_thread())
        return "[ws:item@r1] local private note"

    monkeypatch.setattr(workstreams, "current_turn_note", select, raising=False)
    agent = type("Agent", (), {"platform": "cli"})()

    assert current_workstream_context(agent, "project question") == "[ws:item@r1] local private note"
    assert observed["scope"].profile == str(get_clover_home().resolve())
    assert observed["scope"].project == str(tmp_path.resolve())
    assert observed["scope"].audience == "private"
    assert observed["thread"] is threading.current_thread()


def test_group_context_is_not_treated_as_private(monkeypatch):
    from agent import workstreams

    monkeypatch.setattr(
        workstreams,
        "load_settings",
        lambda: type("S", (), {"enabled": True})(),
        raising=False,
    )
    observed = {}

    def select(scope, query, settings):
        observed["scope"] = scope
        return "" if not scope.private else "should not happen"

    monkeypatch.setattr(workstreams, "current_turn_note", select, raising=False)
    agent = type(
        "Agent",
        (),
        {"platform": "telegram", "_chat_type": "group", "_chat_id": "room-9"},
    )()

    assert current_workstream_context(agent, "question") == ""
    assert observed["scope"].audience == "shared:group:room-9"


def test_dm_scope_is_user_private_and_empty_query_does_not_select(monkeypatch):
    from agent import workstreams

    monkeypatch.setattr(
        workstreams,
        "load_settings",
        lambda: type("S", (), {"enabled": True})(),
        raising=False,
    )
    calls = []
    monkeypatch.setattr(
        workstreams,
        "current_turn_note",
        lambda scope, query, settings: calls.append((scope, query)) or "",
        raising=False,
    )
    agent = type(
        "Agent",
        (),
        {"platform": "telegram", "_chat_type": "dm", "_user_id": "u-7"},
    )()

    assert current_workstream_context(agent, "   ") == ""
    assert not calls
    assert current_workstream_context(agent, "question") == ""
    assert calls[0][0].audience == "private:u-7"


def test_real_turn_persists_exact_workstream_bytes_sent_to_provider(
    provider, monkeypatch, tmp_path
):
    from clover_state import SessionDB

    note = "[ws:rollout@r3] Keep rollout private."
    monkeypatch.setattr(
        "agent.memory_manager.current_workstream_context",
        lambda _agent, _query: note,
    )
    provider.queue[:] = [text("Remembered.")]
    db = SessionDB(tmp_path / "state.db")
    agent = make_agent(
        provider,
        session_db=db,
        session_id="workstream-turn",
        platform="cron",
    )

    agent.run_conversation("What did we decide?", conversation_history=[], task_id="ws-turn")

    wire_user = next(
        message["content"]
        for message in provider.captured[0]["messages"]
        if message.get("role") == "user"
    )
    stored_user = next(
        row for row in db.get_messages("workstream-turn") if row["role"] == "user"
    )
    assert wire_user == "What did we decide?\n\n" + note
    assert stored_user["content"] == "What did we decide?"
    assert stored_user["api_content"] == wire_user


def test_bypass_prologue_composer_sends_identical_workstream_bytes(
    provider, monkeypatch, tmp_path
):
    from agent import turn_context

    note = "[ws:rollout@r3] Keep rollout private."
    monkeypatch.setattr(
        "agent.memory_manager.current_workstream_context",
        lambda _agent, _query: note,
    )
    # Exercise conversation_loop's fallback compose site: the prologue's
    # composition is deliberately unavailable, as for a bypass caller.
    monkeypatch.setattr(turn_context, "compose_user_api_content", lambda *args: None)
    provider.queue[:] = [text("Remembered.")]
    agent = make_agent(provider, session_id="workstream-fallback", platform="cron")

    agent.run_conversation("What did we decide?", conversation_history=[], task_id="ws-fallback")

    wire_user = next(
        message["content"]
        for message in provider.captured[0]["messages"]
        if message.get("role") == "user"
    )
    assert wire_user == "What did we decide?\n\n" + note


def test_local_recall_uses_real_sqlite_and_isolates_profile_project_and_audience(
    monkeypatch, tmp_path
):
    from agent import workstreams
    from cron import notepad

    profile = tmp_path / "profile-a"
    project = tmp_path / "project-a"
    profile.mkdir()
    project.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(profile))
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", tmp_path / "notepad.db")
    settings = workstreams.load_settings(
        {
            "proactive": {
                "enabled": True,
                "private_only": True,
                "recall": {"enabled": True, "max_items": 3, "max_bytes": 1200},
            }
        }
    )
    monkeypatch.setattr(workstreams, "load_settings", lambda: settings)
    scope = workstreams.make_scope(str(project), audience="private:alice", root="alice")
    record = notepad.create_workstream(
        scope,
        kind="decision",
        title="Rollout window",
        summary="Use the Friday rollout window",
        fingerprint="rollout-friday",
        approved=True,
    )
    alice = type(
        "Agent", (), {"platform": "telegram", "_chat_type": "dm", "_user_id": "alice"}
    )()

    note = current_workstream_context(alice, "Which rollout window did we choose?")
    assert "Friday rollout window" in note
    assert record["workstream_id"] in note

    bob = type(
        "Agent", (), {"platform": "telegram", "_chat_type": "dm", "_user_id": "bob"}
    )()
    assert current_workstream_context(bob, "Which rollout window did we choose?") == ""

    group = type(
        "Agent",
        (),
        {
            "platform": "telegram",
            "_chat_type": "group",
            "_chat_id": "room-1",
            "_user_id": "alice",
        },
    )()
    assert current_workstream_context(group, "Which rollout window did we choose?") == ""

    other_project = tmp_path / "project-b"
    other_project.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(other_project))
    assert current_workstream_context(alice, "Which rollout window did we choose?") == ""

    monkeypatch.setenv("TERMINAL_CWD", str(project))
    other_profile = tmp_path / "profile-b"
    other_profile.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(other_profile))
    assert current_workstream_context(alice, "Which rollout window did we choose?") == ""

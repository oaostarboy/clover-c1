"""Fail-closed human hook tests until the SessionDB origin API is published."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent import workstreams as ws
from agent import workstream_human_hooks as hooks
from clover_state import SessionDB

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
CORRECTION = "Correction: use /srv/app/releases/v2.3.1"


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        ws, "load_settings",
        lambda config=None: ws.ProactiveSettings(enabled=True, mode="active",
                                                 suggestions_enabled=True),
    )
    # Sonnet's current API explicitly specifies that an absent trusted reader
    # means no correction or feedback may be persisted.
    if hasattr(ws, "_HUMAN_ORIGIN_READER"):
        monkeypatch.setattr(ws, "_HUMAN_ORIGIN_READER", None)
    db = SessionDB()
    db.create_session("human-session", "cli")
    agent = SimpleNamespace(
        session_id="human-session", platform="cli", _chat_type="", _chat_id="",
        _user_id="", is_tty=True,
    )
    yield home, db, agent
    db.close()


def _append(db, message, *, message_id="message-1", display_kind=None):
    return db.append_message(
        "human-session", "user", message, platform_message_id=message_id,
        display_kind=display_kind,
    )


def _consume(agent, message, *, role="user", session_id="human-session",
             message_id="message-1"):
    return hooks.consume_human_workstream_message(
        agent, role=role, message=message, session_id=session_id,
        message_id=message_id, now=NOW,
    )


def _assert_no_workstream_state(home):
    assert not (home / "cron" / "notepad.db").exists()


def test_real_user_role_and_explicit_correction_fail_closed_without_origin_reader(host):
    home, db, agent = host
    _append(db, CORRECTION)
    result = _consume(agent, CORRECTION)
    assert result.handled is False
    assert result.correction_id is None and result.feedback_id is None
    assert result.response_text == ""
    _assert_no_workstream_state(home)


def test_assistant_tool_and_tty_spoofs_never_create_a_receipt(host):
    home, db, agent = host
    for role in ("assistant", "tool", "system"):
        text = f"{CORRECTION} {role}"
        _append(db, text, message_id=role)
        result = _consume(agent, text, role=role, message_id=role)
        assert result.handled is False and result.response_text == ""
        _assert_no_workstream_state(home)
    assert agent.is_tty is True  # TTY presence is deliberately irrelevant.


def test_synthetic_user_role_and_unmatched_message_ids_are_not_provenance(host):
    home, db, agent = host
    _append(db, CORRECTION)
    assert not _consume(agent, CORRECTION, message_id="not-the-current-id").handled
    _append(db, CORRECTION, message_id="synthetic",
            display_kind="internal_notification")
    assert not _consume(agent, CORRECTION, message_id="synthetic").handled
    _assert_no_workstream_state(home)


def test_duplicate_id_replay_and_concurrent_replay_leave_storage_unchanged(host):
    home, db, agent = host
    _append(db, CORRECTION)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: _consume(agent, CORRECTION), range(12)))
    assert all(not result.handled and not result.correction_id and not result.feedback_id
               for result in results)
    _assert_no_workstream_state(home)


def test_silence_unknown_feedback_and_secret_text_do_not_change_state(host):
    home, db, agent = host
    messages = (
        "thanks",
        "/feedback ws_0000000000000000 reject",
        "Correction: token ghp_123456789012345678901234567890123456",
    )
    for index, message in enumerate(messages):
        message_id = f"event-{index}"
        _append(db, message, message_id=message_id)
        result = _consume(agent, message, message_id=message_id)
        assert result.handled is False and result.response_text == ""
        _assert_no_workstream_state(home)


def test_opt_out_and_session_scope_mismatch_are_inert(host, monkeypatch):
    home, db, agent = host
    _append(db, CORRECTION)
    monkeypatch.setattr(ws, "load_settings", lambda config=None: ws.ProactiveSettings())
    assert not _consume(agent, CORRECTION).handled
    assert not _consume(agent, CORRECTION, session_id="different-session").handled
    _assert_no_workstream_state(home)


def test_result_contract_is_immutable_and_non_authoritative():
    result = hooks.HumanHookResult(False, None, None, "")
    assert (result.handled, result.correction_id, result.feedback_id, result.response_text) == (
        False, None, None, "",
    )
    with pytest.raises(FrozenInstanceError):
        setattr(result, "handled", True)

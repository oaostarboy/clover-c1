"""A mirror round trip keeps one durable conversation and one active surface."""
import asyncio
from types import SimpleNamespace

import pytest

from clover_state import SessionDB
from clover_cli.commands import resolve_command, GATEWAY_KNOWN_COMMANDS


def test_mirror_is_available_on_both_surfaces():
    command = resolve_command("mirror")
    assert command is not None
    assert not command.cli_only and not command.gateway_only
    assert "mirror" in GATEWAY_KNOWN_COMMANDS


def test_cli_gateway_cli_round_trip_preserves_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db._execute_write(lambda conn: conn.execute("INSERT INTO sessions (id, source, title, started_at) VALUES ('same-id', 'cli', 'The conversation', 1)"))
    db.append_message("same-id", "user", "from terminal")
    db.append_message("same-id", "assistant", "hello")
    assert db.request_handoff("same-id", "telegram")
    assert db.claim_handoff("same-id")
    db.complete_handoff("same-id")
    db.append_message("same-id", "user", "from Telegram")
    db.append_message("same-id", "assistant", "welcome back")
    assert db.mirror_to_cli("same-id", "telegram")
    assert db.get_handoff_state("same-id") == {"state": "completed", "platform": "cli", "error": None}
    assert not db.mirror_to_cli("same-id", "telegram")
    assert [m["content"] for m in db.get_messages("same-id")] == [
        "from terminal", "hello", "from Telegram", "welcome back"
    ]
    assert db.request_handoff("same-id", "telegram")
    assert db.claim_handoff("same-id")
    db.complete_handoff("same-id")
    assert db.get_handoff_state("same-id")["platform"] == "telegram"
    assert len(db.list_pending_handoffs()) == 0


def test_mirror_rejects_inflight_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db._execute_write(lambda conn: conn.execute("INSERT INTO sessions (id, source, title, started_at) VALUES ('s', 'cli', 'S', 1)"))
    assert db.request_handoff("s", "telegram")
    assert not db.mirror_to_cli("s", "telegram")
    assert db.get_handoff_state("s")["state"] == "pending"


def test_gateway_mirror_command_releases_current_session(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource

    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db._execute_write(lambda conn: conn.execute("INSERT INTO sessions (id, source, title, started_at) VALUES ('s', 'cli', 'S', 1)"))
    db.request_handoff("s", "telegram")
    db.claim_handoff("s")
    db.complete_handoff("s")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="123")
    event = MessageEvent(text="/mirror cli", source=source)
    runner = object.__new__(GatewayRunner)
    from clover_state import AsyncSessionDB
    runner._session_db = AsyncSessionDB(db)
    monkeypatch.setattr(GatewayRunner, "async_session_store", property(lambda self: SimpleNamespace(get_or_create_session=lambda source: asyncio.sleep(0, result=SimpleNamespace(session_id="s")))))
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:123"
    runner._peek_session_state = lambda key: None
    reply = asyncio.run(runner._handle_mirror_command(event))
    assert "clover --resume s" in reply
    assert db.get_handoff_state("s")["platform"] == "cli"


def test_mirror_refuses_to_release_while_gateway_turn_is_running(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from clover_state import AsyncSessionDB

    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO sessions (id, source, title, started_at) VALUES ('s', 'cli', 'S', 1)"
    ))
    assert db.request_handoff("s", "telegram")
    assert db.claim_handoff("s")
    db.complete_handoff("s")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="123")
    runner = object.__new__(GatewayRunner)
    runner._session_db = AsyncSessionDB(db)
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:123"
    runner._peek_session_state = lambda key: None
    runner._is_session_running = lambda key: True
    monkeypatch.setattr(GatewayRunner, "async_session_store", property(
        lambda self: SimpleNamespace(get_or_create_session=lambda source: asyncio.sleep(
            0, result=SimpleNamespace(session_id="s")
        ))
    ))
    reply = asyncio.run(runner._handle_mirror_command(MessageEvent(text="/mirror cli", source=source)))
    assert "busy" in reply.lower()
    assert db.get_handoff_state("s")["platform"] == "telegram"


def test_compression_child_keeps_surface_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="cli")
    assert db.request_handoff("parent", "telegram")
    assert db.claim_handoff("parent")
    db.complete_handoff("parent")
    db.publish_compression_child(
        parent_session_id="parent", child_session_id="child", source="telegram",
        messages=[{"role": "user", "content": "Context summary"}],
        require_compression_lease=False,
    )
    assert db.resolve_resume_session_id("parent") == "child"
    assert db.get_handoff_state("child")["platform"] == "telegram"
    assert db.mirror_to_cli("child", "telegram")
    assert db.get_handoff_state("child")["platform"] == "cli"


def test_real_gateway_routing_survives_two_mirror_round_trips(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionStore, SessionSource

    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("conversation", source="cli")
    db.append_message("conversation", "user", "first CLI turn")
    db.append_message("conversation", "assistant", "first reply")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="123")
    key = store.get_or_create_session(source).session_key

    for index in range(2):
        assert db.request_handoff("conversation", "telegram")
        assert db.claim_handoff("conversation")
        assert store.switch_session(key, "conversation").session_id == "conversation"
        db.complete_handoff("conversation")
        assert store.peek_session_id(key) == "conversation"
        db.append_message("conversation", "user", f"Telegram turn {index}")
        db.append_message("conversation", "assistant", f"Telegram reply {index}")
        assert db.mirror_to_cli("conversation", "telegram")
        db.append_message("conversation", "user", f"CLI turn {index}")
        db.append_message("conversation", "assistant", f"CLI reply {index}")

    restarted = SessionStore(tmp_path / "sessions", GatewayConfig())
    assert restarted.peek_session_id(key) == "conversation"
    assert db.get_handoff_state("conversation")["platform"] == "cli"
    history = db.get_messages_as_conversation("conversation", repair_alternation=True)
    assert [msg["content"] for msg in history if msg["role"] == "user"] == [
        "first CLI turn", "Telegram turn 0", "CLI turn 0", "Telegram turn 1", "CLI turn 1",
    ]

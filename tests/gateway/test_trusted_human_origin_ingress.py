"""Trusted human ingress must survive the real gateway event dispatch.

Real production path under test: Telegram inbound update -> authorized
adapter event production -> ``BasePlatformAdapter.handle_message`` ->
``GatewayRunner._handle_message`` -> ``_handle_message_with_agent`` ->
``_run_agent`` -> ``AIAgent.run_conversation`` -> SessionDB message row ->
``SessionDB.get_human_origin``.  Only the external provider/network edge is
scripted; the process refuses every socket / DNS attempt.
"""

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from clover_state import AsyncSessionDB, SessionDB
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource

import gateway.run as gateway_run
from run_agent import AIAgent


SESSION_KEY = "agent:main:telegram:dm:12345"
RECEIPT_TABLE = "human_origin_receipts"
REPO = Path(__file__).resolve().parents[2]


# ── hermetic boundary ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    """Fail on every application socket connect / DNS lookup.

    The primary provider is scripted (``AIAgent._create_openai_client``) and
    every auxiliary route is scripted below, so a connection attempt here is a
    leaked provider/network effect, never an expected one. The synchronous
    stdlib socketpair may connect its own private loopback pair on Windows;
    that unaddressable event-loop IPC is not application network access.
    """
    # Desktop server imports prefetch the unrelated release status. Keep that
    # background network edge out of this offline presence/ingress fixture;
    # every application socket/DNS refusal below remains unchanged.
    monkeypatch.setattr('clover_cli.banner.prefetch_update_check', lambda: None)
    attempts = []

    def _refuse(kind):
        def blocked(*args, **kwargs):
            import traceback

            frames = [
                f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                for f in traceback.extract_stack()
                if "site-packages" not in f.filename and "/pytest" not in f.filename
            ][-6:]
            attempts.append((kind, repr(args)[:120], frames))
            raise OSError(f"network attempt refused by test guard: {kind}")
        return blocked

    from tests.native_boundaries import private_socketpair_boundary
    pair, connect = private_socketpair_boundary(
        socket.socketpair, socket.socket.connect, _refuse('connect'))
    monkeypatch.setattr(socket, 'socketpair', pair)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", _refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", _refuse("getaddrinfo"))
    yield attempts
    assert attempts == [], f"unexpected network attempts: {attempts}"


def _user_source():
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="12345",
    )


class _Scripted:
    """Records provider requests; every call answers ``ack``."""

    def __init__(self):
        self.requests = []
        reply = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ack", tool_calls=None),
                finish_reason="stop",
            )],
            model="scripted/test",
            usage=None,
        )
        self.client = MagicMock()

        def create(*args, **kwargs):
            self.requests.append(json.loads(json.dumps(kwargs, default=str)))
            return reply

        self.client.chat.completions.create.side_effect = create

    @property
    def calls(self):
        return len(self.requests)


class Harness:
    """Real runner + real Telegram adapter + real SessionDB in a temp home."""

    def __init__(self, monkeypatch, tmp_path, *, proactive):
        project = tmp_path / "project"
        project.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("TERMINAL_CWD", str(project))
        self.project = project
        home = tmp_path / "profile-home"
        home.mkdir(parents=True, exist_ok=True)
        os_home = tmp_path / "os-home"
        os_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLOVER_HOME", str(home))
        monkeypatch.setenv("HOME", str(os_home))
        self.home = home
        self.db_path = home / "state.db"
        self.scripted = _Scripted()

        runtime = {
            "api_key": "test-key",
            "base_url": "http://127.0.0.1:1/v1",
            "provider": "openai",
            "api_mode": "chat_completions",
        }
        config = {
            "agent": {"disabled_toolsets": ["terminal", "browser"]},
            "display": {"tool_progress": "off", "thinking_progress": False},
            "compression": {"enabled": False},
            "auxiliary": {"background_review": {"enabled": False}, "free_only": True},
        }
        # ``proactive`` is False (off), True (enabled) or a settings mapping.
        # The same section is written to the profile's config.yaml, which is
        # where the agent-side consumers read it from.
        if proactive:
            config["proactive"] = (
                dict(proactive) if isinstance(proactive, dict) else {"enabled": True}
            )
            import yaml

            (home / "config.yaml").write_text(
                yaml.safe_dump({"proactive": config["proactive"]}), encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: dict(runtime))
        monkeypatch.setattr(gateway_run, "_clover_home", home)
        # Context-length probing is a network boundary (local-server / ollama
        # detection). Script every module that imported it by name.
        for target in (
            "agent.model_metadata.get_model_context_length",
            "agent.context_compressor.get_model_context_length",
        ):
            monkeypatch.setattr(target, lambda *_a, **_kw: 100_000)
        for target in (
            "agent.model_metadata.query_ollama_num_ctx",
            "agent.agent_init.query_ollama_num_ctx",
        ):
            monkeypatch.setattr(target, lambda *_a, **_kw: None)
        monkeypatch.setattr(
            AIAgent, "_create_openai_client", lambda *_a, **_kw: self.scripted.client
        )
        # Auxiliary LLM routes (titles, session search, curator ...) never
        # reach a provider: every entry point answers from a local script.
        import agent.auxiliary_client as aux

        for name in ("call_llm", "async_call_llm"):
            if hasattr(aux, name):
                monkeypatch.setattr(aux, name, _aux_refusal(name))

        # A preceding file can import this consumer before aux is scripted.
        # Its from-import binding must use the same refusal boundary, not the
        # real credential/provider resolver. Keep title/persistence logic real.
        import agent.title_generator as titles

        monkeypatch.setattr(titles, "call_llm", aux.call_llm)

        from gateway.session import SessionStore

        self.runner = gateway_run.GatewayRunner(GatewayConfig())
        self.runner._is_user_authorized = lambda _source: True
        self.runner._set_session_env = lambda _context: None
        self.runner._agent_cache = {}
        self.runner._agent_cache_lock = threading.Lock()
        self.runner._resolve_session_agent_runtime = lambda **_kw: (
            "gpt-4o-mini", dict(runtime),
        )
        self.db = SessionDB(db_path=self.db_path)
        self.runner._session_db = AsyncSessionDB(self.db)
        self.runner.session_store = SessionStore(home / "sessions", GatewayConfig())
        # First-contact onboarding is a once-per-install request sidecar that
        # is unrelated to provenance; keep it out of every run so request
        # bytes are comparable.
        monkeypatch.setattr(SessionStore, "has_any_sessions", lambda _self: True)

        from plugins.platforms.telegram.adapter import TelegramAdapter

        self.adapter = TelegramAdapter(PlatformConfig(
            enabled=True, token="fake-token", extra={"allow_from": ["12345"]},
        ))
        self.adapter._text_batch_delay_seconds = 0.01
        self.adapter._text_batch_split_delay_seconds = 0.01
        self.adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="out-1"))
        self.adapter.set_message_handler(self.runner._handle_message)
        self.runner.adapters = {Platform.TELEGRAM: self.adapter}
        # Observe (never replace) the real dispatch into the agent run.
        self.run_agent_kwargs = []
        real_run_agent = self.runner._run_agent

        async def _spy(*args, **kwargs):
            self.run_agent_kwargs.append(kwargs)
            return await real_run_agent(*args, **kwargs)

        self.runner._run_agent = _spy

    def update(self, text, *, message_id=42, from_user_id=12345):
        message = SimpleNamespace(
            message_id=message_id, text=text, caption=None, entities=[],
            caption_entities=[], message_thread_id=None, is_topic_message=False,
            chat=SimpleNamespace(id=12345, type="private", title=None, is_forum=False),
            from_user=SimpleNamespace(id=from_user_id, full_name="Ant", first_name="Ant",
                                      username="ant", is_bot=False),
            reply_to_message=None, date=None, location=None, photo=None, video=None,
            audio=None, voice=None, document=None, sticker=None, media_group_id=None,
        )
        return SimpleNamespace(update_id=message_id, message=message, effective_message=None)

    async def settle(self, calls=1, timeout=15.0):
        """Wait until the scripted provider answered ``calls`` and the reply was sent."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.scripted.calls >= calls and self.adapter.send.await_count >= calls:
                await asyncio.sleep(0.2)
                return
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"turn did not settle: provider={self.scripted.calls} send={self.adapter.send.await_count}"
        )

    def inbound_human(self, text, **kw):
        expected = self.scripted.calls + 1

        async def _run():
            await self.adapter._handle_text_message(self.update(text, **kw), SimpleNamespace())
            await self.settle(calls=expected)
        asyncio.run(_run())

    def dispatch(self, *events):
        """Drive each event through the real gateway entry
        (``GatewayRunner._handle_message``) in one event loop."""

        async def _run():
            for event in events:
                await self.runner._handle_message(event)

        asyncio.run(_run())

    def dispatch_with_generation(self, event, generation, source=None):
        """Real ``_handle_message_with_agent`` with an explicit run generation."""
        source = source or _user_source()
        asyncio.run(self.runner._handle_message_with_agent(
            event, source, SESSION_KEY, generation))

    def user_rows(self):
        return [r for r in self.db.get_messages(self.session_id()) if r.get("role") == "user"]

    def session_id(self):
        entry = self.runner.session_store._entries[SESSION_KEY]
        return entry.session_id

    def tables(self):
        with sqlite3.connect(self.db_path) as conn:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}

    def close(self):
        # Complete real background title work before closing its SQLite owner
        # and before monkeypatch removes the scripted auxiliary boundary.
        for worker in threading.enumerate():
            if worker.name == "auto-title":
                worker.join(timeout=5)
                assert not worker.is_alive(), "scripted auto-title worker did not finish"
        self.db.close()


def _aux_refusal(name):
    async def _async(*_a, **_kw):
        raise RuntimeError(f"auxiliary {name} is scripted off in this test")

    def _sync(*_a, **_kw):
        raise RuntimeError(f"auxiliary {name} is scripted off in this test")

    return _async if name.startswith("async") else _sync


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=True)
    yield h
    h.close()


@pytest.fixture
def disabled(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=False)
    yield h
    h.close()


# ── positive: the real inbound human message gets exactly one receipt ───────


def test_real_inbound_human_message_persists_origin_receipt(enabled):
    enabled.inbound_human("remember my preference")

    rows = enabled.user_rows()
    assert [r["content"] for r in rows] == ["remember my preference"]
    assert enabled.scripted.calls == 1

    receipt = enabled.db.get_human_origin(enabled.session_id(), rows[0]["id"])
    assert receipt is not None
    assert receipt.origin == "human"
    assert receipt.session_id == enabled.session_id()
    assert receipt.message_row_id == rows[0]["id"]
    assert receipt.consumed is False
    assert receipt.profile == str(enabled.home.resolve())
    # An opaque host-minted id: never the platform message id or the text.
    assert receipt.ingress_event_id
    assert receipt.ingress_event_id not in {"42", "remember my preference"}

    # Readable from a brand-new SessionDB handle on the same file.
    fresh = SessionDB(db_path=enabled.db_path)
    try:
        assert fresh.get_human_origin(enabled.session_id(), rows[0]["id"]) == receipt
    finally:
        fresh.close()


def test_receipt_reads_back_in_a_fresh_process(enabled):
    enabled.inbound_human("remember my preference")
    row = enabled.user_rows()[0]
    code = (
        "import json, sys\n"
        "from clover_state import SessionDB\n"
        "db = SessionDB(db_path=sys.argv[1], read_only=True)\n"
        "r = db.get_human_origin(sys.argv[2], int(sys.argv[3]))\n"
        "print(json.dumps(None if r is None else [r.profile, r.session_id,"
        " r.message_row_id, r.ingress_event_id, r.origin, r.consumed]))\n"
    )
    env = {"PATH": "/usr/bin", "CLOVER_HOME": str(enabled.home), "HOME": str(enabled.home),
           "CLOVER_TEST_ISOLATION": str(enabled.home)}
    if os.name == 'nt':
        # Keep the home private while retaining Windows' stdlib DLL location.
        env.update(USERPROFILE=str(enabled.home), SYSTEMROOT=os.environ['SYSTEMROOT'])
    out = subprocess.run(
        [sys.executable, "-c", code, str(enabled.db_path), enabled.session_id(), str(row["id"])],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    profile, session_id, row_id, event_id, origin, consumed = json.loads(out.stdout.strip().splitlines()[-1])
    local = enabled.db.get_human_origin(enabled.session_id(), row["id"])
    assert (profile, session_id, row_id, event_id, origin, consumed) == (
        local.profile, local.session_id, local.message_row_id,
        local.ingress_event_id, "human", False,
    )


# ── negatives through the same real path ───────────────────────────────────


def _receipts(h):
    """Every receipt row, read straight from SQLite (not via the accessor)."""
    if RECEIPT_TABLE not in h.tables():
        return []
    with sqlite3.connect(h.db_path) as conn:
        return conn.execute(
            f"SELECT ingress_event_id, session_id, message_row_id FROM {RECEIPT_TABLE}"
        ).fetchall()


def test_gateway_passes_capability_only_for_the_human_turn(enabled):
    enabled.inbound_human("remember my preference")
    assert len(enabled.run_agent_kwargs) == 1
    ingress = enabled.run_agent_kwargs[0].get("trusted_human_ingress")
    assert ingress is not None
    assert ingress.event_id != "42"  # never the platform message id
    assert ingress.session_id == enabled.session_id()

    async def _internal():
        event = MessageEvent(
            text="helper completion", source=_user_source(), message_id="platform-456",
            internal=True,
        )
        await enabled.adapter.handle_message(event)
        await enabled.settle(calls=2)

    asyncio.run(_internal())
    assert enabled.run_agent_kwargs[-1].get("trusted_human_ingress") is None
    rows = enabled.user_rows()
    assert len(rows) == 2
    assert enabled.db.get_human_origin(enabled.session_id(), rows[0]["id"]) is not None
    assert enabled.db.get_human_origin(enabled.session_id(), rows[1]["id"]) is None
    assert len(_receipts(enabled)) == 1


def test_event_fields_never_mint_provenance(enabled):
    """An ordinary MessageEvent that merely looks like a Telegram human message."""
    source = _user_source()
    event = MessageEvent(
        text="remember my preference", source=source, message_id="platform-123",
        user_id="12345", user_name="Ant", internal=False,
        metadata={"gateway_session_key": SESSION_KEY, "human": True, "origin": "human"},
    )
    enabled.dispatch(event)
    assert enabled.scripted.calls == 1
    assert len(enabled.user_rows()) == 1
    assert "trusted_human_ingress" not in enabled.run_agent_kwargs[0]
    assert _receipts(enabled) == []


def test_copied_or_rebuilt_event_has_no_provenance(enabled):
    import copy
    import dataclasses

    from agent.workstream_origin import mint_human_ingress

    source = _user_source()
    original = MessageEvent(text="remember my preference", source=source, message_id="m-1")
    assert mint_human_ingress(original, platform="telegram") is True
    enabled.dispatch(
        copy.copy(original), dataclasses.replace(original), copy.deepcopy(original)
    )
    assert all("trusted_human_ingress" not in kw for kw in enabled.run_agent_kwargs)
    assert _receipts(enabled) == []


def test_minted_event_is_single_use_and_replay_is_unknown(enabled):
    from agent.workstream_origin import mint_human_ingress

    source = _user_source()
    event = MessageEvent(text="first", source=source, message_id="m-1")
    assert mint_human_ingress(event, platform="telegram") is True
    assert mint_human_ingress(event, platform="telegram") is False  # no double mint
    enabled.dispatch(event, event)
    assert len([kw for kw in enabled.run_agent_kwargs if "trusted_human_ingress" in kw]) == 1
    assert len(_receipts(enabled)) == 1


def test_synthetic_event_cannot_be_minted_even_by_a_buggy_producer(enabled):
    from agent.workstream_origin import mint_human_ingress

    source = _user_source()
    event = MessageEvent(text="wake", source=source, message_id="w-1", internal=True)
    assert mint_human_ingress(event, platform="telegram") is False
    assert mint_human_ingress(object(), platform="telegram") is False
    assert mint_human_ingress(MessageEvent(text="x", source=source), platform="") is False


def test_platform_mismatch_does_not_redeem(enabled):
    from agent.workstream_origin import mint_human_ingress

    source = _user_source()
    event = MessageEvent(text="remember", source=source, message_id="m-1")
    assert mint_human_ingress(event, platform="discord") is True  # minted by another adapter
    enabled.dispatch(event)
    assert "trusted_human_ingress" not in enabled.run_agent_kwargs[0]
    assert _receipts(enabled) == []


def test_stale_generation_yields_no_receipt(enabled):
    """A turn whose run generation was invalidated (Stop / /new) gets none."""
    from agent.workstream_origin import mint_human_ingress

    source = _user_source()
    event = MessageEvent(text="remember my preference", source=source, message_id="m-1")
    assert mint_human_ingress(event, platform="telegram") is True
    stale = enabled.runner._begin_session_run_generation(SESSION_KEY)
    enabled.runner._invalidate_session_run_generation(SESSION_KEY, reason="stop")
    enabled.dispatch_with_generation(event, stale)
    assert len(enabled.user_rows()) == 1  # the turn ran and persisted its row
    assert _receipts(enabled) == []


def test_cancelled_turn_before_persist_writes_no_receipt(enabled):
    """/stop lands between redemption and the row write: provenance is dropped."""
    from agent import workstream_origin
    from agent.workstream_origin import mint_human_ingress

    source = _user_source()
    event = MessageEvent(text="remember my preference", source=source, message_id="m-1")
    assert mint_human_ingress(event, platform="telegram") is True
    generation = enabled.runner._begin_session_run_generation(SESSION_KEY)
    original = workstream_origin.ingress_is_valid

    def _stop_then_check(ingress, **kw):
        enabled.runner._invalidate_session_run_generation(SESSION_KEY, reason="stop")
        return original(ingress, **kw)

    import clover_state  # noqa: F401  (write path resolves the module attribute)
    workstream_origin.ingress_is_valid = _stop_then_check
    try:
        enabled.dispatch_with_generation(event, generation)
    finally:
        workstream_origin.ingress_is_valid = original
    assert len(enabled.user_rows()) == 1
    assert _receipts(enabled) == []


def test_cached_agent_never_replays_a_prior_capability(enabled, monkeypatch):
    seen = []
    real = AIAgent.run_conversation

    def _observe(self, *args, **kwargs):  # wraps the real method
        seen.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(AIAgent, "run_conversation", _observe)
    enabled.inbound_human("remember my preference")
    assert len(seen) == 1
    assert seen[0]._trusted_human_ingress is None

    async def _internal():
        await enabled.adapter.handle_message(MessageEvent(
            text="helper completion", source=_user_source(), message_id="p-2", internal=True))
        await enabled.settle(calls=2)

    asyncio.run(_internal())
    rows = enabled.user_rows()
    assert len(rows) == 2
    assert enabled.db.get_human_origin(enabled.session_id(), rows[1]["id"]) is None
    assert len(_receipts(enabled)) == 1


def test_role_only_user_rows_cannot_create_a_receipt(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("sess-origin", source="telegram")
    db.append_message("sess-origin", "user", "same text")
    db.append_message("sess-origin", "user", "same text")
    try:
        assert db.get_human_origin("sess-origin", 1) is None
        assert db.get_human_origin("sess-origin", 2) is None
        assert db.get_human_origin("another-session", 1) is None
        assert RECEIPT_TABLE not in {
            r[0] for r in sqlite3.connect(tmp_path / "state.db").execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        db.close()


# ── disabled mode ──────────────────────────────────────────────────────────


_FIRST_CONTACT = "\n\n[System note: This is the user's very first message ever."


def _wire(request):
    """Provider request minus the system prompt (volatile) and minus the
    gateway's once-per-install first-contact onboarding sidecar, which is
    unrelated to this feature and depends on process-global install state."""
    messages = []
    for message in request.get("messages", []):
        if message.get("role") == "system":
            continue
        content = message.get("content")
        if isinstance(content, str) and _FIRST_CONTACT in content:
            message = {**message, "content": content.split(_FIRST_CONTACT)[0]}
        messages.append(message)
    return {k: v for k, v in request.items() if k != "messages"}, messages


def test_disabled_mode_writes_nothing_and_changes_no_request_bytes(monkeypatch, tmp_path):
    off = Harness(monkeypatch, tmp_path / "off", proactive=False)
    try:
        off.inbound_human("remember my preference")
        off_rows = off.user_rows()
        assert off.scripted.calls == 1
        assert RECEIPT_TABLE not in off.tables()
        assert off.db.get_human_origin(off.session_id(), off_rows[0]["id"]) is None
        assert RECEIPT_TABLE not in off.tables()  # the read did not create it
        assert all("trusted_human_ingress" not in kw for kw in off.run_agent_kwargs)
        off_request = _wire(off.scripted.requests[0])
        off_content = [r["content"] for r in off_rows]
    finally:
        off.close()

    on = Harness(monkeypatch, tmp_path / "on", proactive=True)
    try:
        on.inbound_human("remember my preference")
        on_request = _wire(on.scripted.requests[0])
        assert [r["content"] for r in on.user_rows()] == off_content
    finally:
        on.close()

    off_kwargs, off_messages = off_request
    on_kwargs, on_messages = on_request
    assert on_messages == off_messages
    assert {k: v for k, v in on_kwargs.items() if k != "extra_body"} == {
        k: v for k, v in off_kwargs.items() if k != "extra_body"
    }

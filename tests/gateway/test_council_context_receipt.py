"""A council verdict shown in chat must also be in the parent's conversation.

The gateway posts the final council answer straight through the adapter, so the
launching session's transcript never sees it. After a confirmed card the gateway
records one internal row on the conversation the user is actually looking at.
Real ``SessionStore`` + ``SessionDB`` against a temp ``CLOVER_HOME``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.agent_runtime_helpers import repair_message_sequence
from gateway.config import GatewayConfig, Platform
from gateway.council_progress import CouncilRunWatcher
from gateway.platforms.base import SendResult
from gateway.run import _build_gateway_agent_history
from gateway.session import SessionSource, SessionStore
from gateway.slash_commands import GatewaySlashCommandsMixin

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = (
    ROOT / "optional-skills" / "autonomous-ai-agents" / "council" / "scripts" / "council_run.py"
)
VERDICT = "UNIQUE-VERDICT-XYZ"
RUN_ID = "council-ctx-1"


def _load_runner():
    spec = importlib.util.spec_from_file_location("council_run_context", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Adapter:
    """Recording adapter; ``edit_message`` is what lets the v2 final edit in place."""

    def __init__(self, edit_raises: bool = False):
        self.sends = []
        self.edits = []
        self.updates = []
        self._edit_raises = edit_raises

    async def send_or_update_status(self, chat_id, status_key, content, metadata=None):
        self.updates.append((chat_id, status_key, content))

    async def send(self, chat_id, content, metadata=None):
        self.sends.append((chat_id, content))
        return SendResult(success=True, message_id=str(len(self.sends)))

    async def edit_message(self, chat_id, message_id, content, finalize=False, metadata=None):
        self.edits.append((chat_id, message_id, content))
        if self._edit_raises:
            raise RuntimeError("connection lost after the edit was sent")
        return SendResult(success=True, message_id=message_id)


class Host(GatewaySlashCommandsMixin):
    """The slice of GatewayRunner the council watcher construction needs."""

    def __init__(self, home: Path, store: SessionStore, adapter: Adapter):
        self._home = home
        self.session_store = store
        self._adapter = adapter
        self.running_keys: set[str] = set()

    def _is_session_running(self, session_key: str) -> bool:
        return session_key in self.running_keys

    def _council_scan_homes(self):
        return [self._home]

    def _council_card_style(self, platform):
        return "v2"

    def _council_run_target(self, origin):
        return self._adapter, origin["chat_id"], {"thread_id": origin["thread_id"]}


class Env:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.home = tmp_path / "home"
        self.work = self.home / "council" / "runs" / RUN_ID
        self.work.mkdir(parents=True)
        sessions = tmp_path / "sessions"
        self.store = SessionStore(sessions_dir=sessions, config=GatewayConfig())
        if self.store._db is not None:
            self.store._db.close()
        from clover_state import SessionDB

        self.store._db = SessionDB(db_path=tmp_path / "state.db")
        self.source = SessionSource(
            platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="u1", thread_id="7"
        )
        self.entry = self.store.get_or_create_session(self.source)
        self.key = self.entry.session_key
        self.session_id = self.entry.session_id
        self.adapter = Adapter()
        self.host = Host(self.home, self.store, self.adapter)
        self.runner = _load_runner()
        for name, value in {
            "CLOVER_SESSION_PLATFORM": "telegram",
            "CLOVER_SESSION_CHAT_ID": "123",
            "CLOVER_SESSION_THREAD_ID": "7",
            "CLOVER_SESSION_KEY": self.key,
            "CLOVER_SESSION_ID": self.session_id,
        }.items():
            monkeypatch.setenv(name, value)
        # The launching turn: the agent asked for a council and the tool returned.
        self.add("user", "Run a council on pricing.")
        self.add("assistant", None, tool_calls=[_call("c1")])
        self.add("tool", "started", tool_call_id="c1")
        self.add("assistant", "The council is running.")

    def add(self, role, content, session_id=None, **extra):
        self.store.append_to_transcript(
            session_id or self.session_id, {"role": role, "content": content, **extra}
        )

    def launch(self, with_session_id=True):
        if not with_session_id:
            # What a live gateway terminal child sees: the key, but no id.
            self.monkeypatch.delenv("CLOVER_SESSION_ID")
        assert self.runner.write_origin(self.work) is True
        assert (json.loads((self.work / "origin.json").read_text()).get("session_id") is not None) is with_session_id
        self.progress("arguments", seats=1)

    def progress(self, stage, status="running", seats=0):
        self.runner.write_progress(
            self.work, status=status, mode="full", stage=stage, seat_done=seats, seat_total=5,
            review_done=0, review_total=5, started_at=100.0, now=100.0 + seats,
        )

    def finish_run(self, status="done"):
        if status == "done":
            (self.work / "summary.json").write_text(
                json.dumps({
                    "mode": "full", "verdict": VERDICT, "why": "Pilot first.",
                    "caveat": "Scale later.", "stalled": [],
                    "attack_severity": "LOW", "ruling": "Attack rejected.",
                }),
                encoding="utf-8",
            )
        self.progress("chairman" if status == "done" else "seats", status=status, seats=5)

    def watcher(self, adapter=None, poll_s=0.01):
        if adapter is not None:
            self.host._adapter = adapter
        return self.host._make_council_watcher(poll_s=poll_s)

    def history(self, session_id=None):
        rows = self.store.load_transcript(session_id or self.session_id)
        return _build_gateway_agent_history(rows)[0]

    def council_rows(self, session_id=None):
        return [
            m for m in self.store.load_transcript(session_id or self.session_id)
            if str(m.get("content") or "").startswith("[Council")
        ]

    def count(self, session_id=None):
        return json.dumps(self.history(session_id)).count(VERDICT)

    async def run_to_end(self, status="done", watcher=None):
        watcher = watcher or self.watcher()
        (task,) = watcher.scan_once()
        await asyncio.sleep(0.05)
        self.finish_run(status)
        await asyncio.wait_for(task, 5)
        return watcher


def _call(call_id):
    return {"id": call_id, "type": "function", "function": {"name": "terminal", "arguments": "{}"}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def _roles_alternate(messages):
    return all(a["role"] != b["role"] or a["role"] == "tool" for a, b in zip(messages, messages[1:]))


@pytest.mark.asyncio
async def test_final_shown_in_chat_is_in_the_conversation_once(env):
    env.launch()
    await env.run_to_end()

    assert env.count() == 1
    rows = env.council_rows()
    assert len(rows) == 1 and rows[0]["role"] == "user"
    text = rows[0]["content"]
    assert "do not repost" in text
    for needle in ("VERDICT", "WHY", "CAVEAT", "ATTACK", "Pilot first.", "Scale later."):
        assert needle in text
    # Nothing extra reaches the user: one live card send, one in-place final edit.
    assert len(env.adapter.sends) == 1 and len(env.adapter.edits) == 1
    assert (env.work / "gateway-context.json").exists()

    env.add("user", "Explain better.")
    history = env.history()
    assert env.count() == 1
    repair_message_sequence(SimpleNamespace(), history)
    assert _roles_alternate(history)
    assert history[-1]["role"] == "user"
    assert VERDICT in history[-1]["content"] and "Explain better." in history[-1]["content"]


@pytest.mark.asyncio
async def test_new_session_between_launch_and_final_gets_nothing(env, caplog):
    env.launch()
    watcher = env.watcher()
    (task,) = watcher.scan_once()
    await asyncio.sleep(0.05)
    new_entry = env.store.reset_session(env.key)
    assert new_entry.session_id != env.session_id
    env.finish_run()
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(task, 5)

    assert env.count(new_entry.session_id) == 0
    assert env.count(env.session_id) == 0
    assert any(
        r.levelno == logging.INFO and "council" in r.getMessage().lower()
        for r in caplog.records
    )
    assert len(env.adapter.sends) == 1  # the user still got the card


@pytest.mark.asyncio
async def test_origin_with_neither_session_id_nor_key_is_skipped(env, caplog):
    env.launch()
    origin = json.loads((env.work / "origin.json").read_text())
    origin.pop("session_id", None)
    origin.pop("session_key", None)
    (env.work / "origin.json").write_text(json.dumps(origin))
    with caplog.at_level(logging.INFO):
        await env.run_to_end()
    assert env.count() == 0
    assert any(r.levelno == logging.INFO for r in caplog.records)


@pytest.mark.asyncio
async def test_agent_launch_without_session_id_binds_the_live_session_once(env):
    env.launch(with_session_id=False)
    await env.run_to_end()

    assert env.count() == 1
    assert len(env.council_rows()) == 1
    observed = json.loads((env.work / "gateway-origin-session.json").read_text())
    assert observed["session_id"] == env.session_id and observed["observed_at"] > 0


@pytest.mark.asyncio
async def test_discovery_after_new_session_is_skipped(env, caplog):
    env.launch(with_session_id=False)
    new_entry = env.store.reset_session(env.key)  # /new before the watcher first sees the run
    with caplog.at_level(logging.INFO):
        await env.run_to_end()

    observed = json.loads((env.work / "gateway-origin-session.json").read_text())
    assert observed["session_id"] is None and observed["reason"]
    assert env.count(new_entry.session_id) == 0 and env.count(env.session_id) == 0
    assert any(r.levelno == logging.INFO for r in caplog.records)
    assert len(env.adapter.sends) == 1  # the user still got the card


@pytest.mark.asyncio
async def test_session_created_after_launch_is_not_bound(env):
    env.launch(with_session_id=False)
    origin = json.loads((env.work / "origin.json").read_text())
    origin["created_at"] = time.time() - 3600  # the run began long before this session existed
    (env.work / "origin.json").write_text(json.dumps(origin))
    await env.run_to_end()

    observed = json.loads((env.work / "gateway-origin-session.json").read_text())
    assert observed["session_id"] is None
    assert env.count() == 0


@pytest.mark.asyncio
async def test_ended_session_is_not_bound(env):
    env.launch(with_session_id=False)
    env.store._db.end_session(env.session_id, "user_exit")
    await env.run_to_end()

    assert json.loads((env.work / "gateway-origin-session.json").read_text())["session_id"] is None
    assert env.count() == 0


@pytest.mark.asyncio
async def test_restart_reuses_the_observed_binding(env):
    env.launch(with_session_id=False)
    await env.run_to_end()
    observed_path = env.work / "gateway-origin-session.json"
    first = observed_path.read_text()
    (env.work / "gateway-context.json").unlink()  # crash after the append

    tasks = env.watcher().scan_once()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)

    assert observed_path.read_text() == first  # stamped once, never re-observed
    assert env.count() == 1


@pytest.mark.asyncio
async def test_origin_records_the_launching_session(env):
    env.launch()
    origin = json.loads((env.work / "origin.json").read_text())
    assert origin["session_id"] == env.session_id
    assert origin["session_key"] == env.key


@pytest.mark.asyncio
async def test_restart_with_receipt_does_not_duplicate(env):
    env.launch()
    await env.run_to_end()
    assert env.count() == 1

    fresh = env.watcher()
    assert fresh.scan_once() == []
    await asyncio.sleep(0.05)
    assert env.count() == 1


@pytest.mark.asyncio
async def test_crash_between_append_and_receipt_does_not_duplicate(env):
    env.launch()
    await env.run_to_end()
    (env.work / "gateway-context.json").unlink()  # the crash window

    fresh = env.watcher()
    tasks = fresh.scan_once()
    assert len(tasks) == 1
    await asyncio.wait_for(tasks[0], 5)
    assert env.count() == 1
    assert (env.work / "gateway-context.json").exists()
    assert len(env.adapter.sends) == 1 and len(env.adapter.edits) == 1  # nothing resent


@pytest.mark.asyncio
async def test_restart_after_ack_but_before_append_writes_the_row(env):
    env.launch()
    env.finish_run()
    (env.work / "gateway-card.json").write_text("{}")  # card shown, then the gateway died

    fresh = env.watcher()
    tasks = fresh.scan_once()
    assert len(tasks) == 1
    await asyncio.wait_for(tasks[0], 5)
    assert env.count() == 1
    assert env.adapter.sends == [] and env.adapter.edits == []  # no card replay


@pytest.mark.asyncio
async def test_ambiguous_send_is_recorded_as_uncertain_and_not_resent(env):
    env.launch()
    adapter = Adapter(edit_raises=True)
    watcher = env.watcher(adapter)
    await env.run_to_end(watcher=watcher)

    assert (env.work / "gateway-card-uncertain.json").exists()
    assert env.count() == 1
    (row,) = env.council_rows()
    assert "(delivery uncertain)" in row["content"]
    assert len(adapter.edits) == 1  # never retried


@pytest.mark.asyncio
async def test_busy_session_gets_the_row_only_after_it_goes_idle(env):
    env.launch()
    env.add("assistant", None, tool_calls=[_call("c2")])  # mid tool chain: no result yet
    env.host.running_keys.add(env.key)
    watcher = env.watcher()
    (task,) = watcher.scan_once()
    await asyncio.sleep(0.05)
    env.finish_run()
    await asyncio.sleep(0.3)
    assert not task.done()
    assert env.count() == 0  # nothing between assistant(tool_calls) and its tool row

    env.add("tool", "ok", tool_call_id="c2")
    env.host.running_keys.discard(env.key)
    await asyncio.wait_for(task, 5)

    rows = env.store.load_transcript(env.session_id)
    assert env.count() == 1
    last_tool = max(i for i, m in enumerate(rows) if m["role"] == "tool")
    council = next(i for i, m in enumerate(rows) if VERDICT in str(m.get("content")))
    assert council > last_tool
    history = env.history()
    assert _roles_alternate(history)


@pytest.mark.asyncio
async def test_active_turn_lease_defers_the_row_until_released(env):
    env.launch()
    db = env.store._db
    holder = f"pid={os.getpid()} turn"
    assert db.try_acquire_session_turn_lease(env.session_id, holder, ttl_seconds=60)
    watcher = env.watcher()
    (task,) = watcher.scan_once()
    await asyncio.sleep(0.05)
    env.finish_run()
    await asyncio.sleep(0.3)
    assert not task.done() and env.count() == 0

    db.release_session_turn_lease(env.session_id, holder)
    await asyncio.wait_for(task, 5)
    assert env.count() == 1


@pytest.mark.asyncio
async def test_never_idle_until_the_watch_lifetime_ends_is_reported_not_dropped(env, caplog, monkeypatch):
    monkeypatch.setattr(CouncilRunWatcher, "MAX_RUN_AGE_S", 1.0)
    env.launch()
    env.host.running_keys.add(env.key)
    watcher = env.watcher()
    (task,) = watcher.scan_once()
    await asyncio.sleep(0.05)
    env.finish_run()
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(task, 5)
    assert env.count() == 0
    assert (env.work / "gateway-context-failed.json").exists()
    assert not (env.work / "gateway-context.json").exists()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_failure_card_is_recorded(env):
    env.launch()
    await env.run_to_end(status="failed")
    rows = [m for m in env.store.load_transcript(env.session_id) if str(m.get("content")).startswith("[Council")]
    assert len(rows) == 1 and rows[0]["role"] == "user"
    assert "fail" in rows[0]["content"].lower()
    assert VERDICT not in rows[0]["content"]


@pytest.mark.asyncio
async def test_compression_between_launch_and_final_lands_on_the_tip(env):
    env.launch()
    watcher = env.watcher()
    (task,) = watcher.scan_once()
    await asyncio.sleep(0.05)
    env.store._db.publish_compression_child(
        parent_session_id=env.session_id,
        child_session_id="compressed-child",
        source="telegram",
        messages=[{"role": "user", "content": "compressed history"}],
        require_compression_lease=False,
    )
    with env.store._lock:
        env.store._entries[env.key].session_id = "compressed-child"
    env.finish_run()
    await asyncio.wait_for(task, 5)

    def stored(session_id):
        return [m for m in env.store._db.get_messages(session_id) if VERDICT in str(m.get("content"))]

    assert len(stored("compressed-child")) == 1
    assert stored(env.session_id) == []


@pytest.mark.asyncio
async def test_typed_council_final_is_recorded_in_the_typing_session(env):
    script = env.home / "skills" / "autonomous-ai-agents" / "council" / "scripts" / "council_run.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, os, pathlib, sys\n"
        "args=sys.argv[1:]\n"
        "run_id=args[args.index('--id')+1]\n"
        "work=pathlib.Path(os.environ['CLOVER_HOME'])/'council'/'runs'/run_id\n"
        "work.mkdir(parents=True, exist_ok=True)\n"
        "(work/'summary.json').write_text(json.dumps({'mode':'quick','verdict':'%s','why':'Pilot.',"
        "'caveat':'Scale.','stalled':[],'attack_severity':'','ruling':''}))\n"
        "(work/'progress.json').write_text(json.dumps({'status':'done','mode':'quick','stage':'chairman',"
        "'seat_done':3,'seat_total':3,'review_done':0,'review_total':0,'elapsed_s':1}))\n" % VERDICT,
        encoding="utf-8",
    )
    host = env.host
    host._resolve_profile_home_for_source = lambda source: env.home
    host._thread_metadata_for_source = lambda source: {"thread_id": "7"}
    host._adapter_for_source = lambda source: env.adapter
    host._session_key_for_source = lambda source: env.store._generate_session_key(source)
    event = SimpleNamespace(source=env.source, get_command_args=lambda: "quick Is this clean?")

    assert await host._handle_council_command(event) is None
    await asyncio.wait_for(asyncio.gather(*host._council_context_tasks), 5)

    assert env.count() == 1
    (row,) = env.council_rows()
    assert row["role"] == "user" and "do not repost" in row["content"]

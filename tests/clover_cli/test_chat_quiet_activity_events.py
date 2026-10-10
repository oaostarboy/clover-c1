"""``clover --activity-events chat --oneshot -Q -q …`` must still emit activity.

Agent-card workers launch the chat single-query path with ``-Q``. The quiet
branch nulls every display callback so stdout carries only the answer, and it
used to drop the ``--activity-events`` stream with them: the parent's card sat
on "no new output" for the whole run. ``-Q`` hides human chatter; it must not
hide the structured stderr stream the caller explicitly asked for.

Drives the real argv -> ``clover_cli.main.main`` -> ``cmd_chat`` ->
``cli.main`` path; only the CLI object (provider/agent construction) is faked.
"""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest


def _events(stderr_text: str) -> list[dict]:
    out = []
    for line in stderr_text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            obj = json.loads(line)
            if obj.get("clover_activity") == 1:
                out.append(obj)
    return out


def _fake_cli_class(calls):
    class FakeCLI:
        def __init__(self, **_kwargs):
            self.provider = "test-provider"
            self.model = "test-model"
            self.session_id = "quiet-session"
            self.conversation_history = []
            self._active_agent_route_signature = "same-route"
            self.agent = None

        def _claim_active_session(self, surface, *, stderr=False):
            return True

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, effective_query):
            return {"signature": "same-route", "model": None, "runtime": None,
                    "request_overrides": None}

        def _init_agent(self, **kwargs):
            agent = SimpleNamespace(
                session_id="quiet-session",
                platform="cli",
                model="test-model",
                quiet_mode=False,
                suppress_status_output=False,
                stream_delta_callback=object(),
                tool_gen_callback=object(),
                tool_progress_callback=lambda *a, **k: calls.append("human-progress"),
                interim_assistant_callback=None,
                model_fallback_callback=None,
            )

            def run_conversation(*, user_message, conversation_history):
                cb = agent.tool_progress_callback
                if cb is not None:
                    cb("tool.started", "terminal", "pytest -q", {"command": "pytest -q"},
                       tool_call_id="c1")
                    cb("tool.completed", "terminal", None, None, duration=1.5,
                       is_error=False, tool_call_id="c1")
                icb = agent.interim_assistant_callback
                if icb is not None:
                    icb("Checking the scheduler lock next.")
                return {"final_response": "all green", "completed": True}

            agent.run_conversation = run_conversation
            self.agent = agent
            return True

    return FakeCLI


@pytest.fixture
def chat_entrypoint(monkeypatch):
    import cli as cli_mod
    import clover_cli.main as main_mod

    calls: list = []
    monkeypatch.setattr(cli_mod, "CloverCLI", _fake_cli_class(calls))
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_k: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda _args: None)
    monkeypatch.setattr(main_mod, "_confirm_startup_expensive_model_override", lambda _a: None)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    monkeypatch.delenv("CLOVER_KANBAN_TASK", raising=False)
    monkeypatch.delenv("CLOVER_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.delenv("CLOVER_TUI", raising=False)

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        with pytest.raises(SystemExit) as exc:
            main_mod.main()
        return exc.value.code, out.getvalue(), err.getvalue(), calls

    return run


def test_quiet_chat_oneshot_still_emits_activity_events(chat_entrypoint):
    code, out, err, calls = chat_entrypoint(
        ["clover", "--activity-events", "chat", "--oneshot", "-Q", "-q", "run the tests"]
    )

    assert code == 0
    events = _events(err)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start"
    assert "tool.started" in kinds and "tool.completed" in kinds
    assert "note" in kinds
    assert kinds[-1] == "result"
    assert events[-1]["status"] == "completed"
    started = next(e for e in events if e["event"] == "tool.started")
    assert started["tool"] == "terminal"
    # -Q still hides human chatter: stdout is only the answer, and the
    # human progress renderer was not invoked.
    assert out.strip() == "all green"
    assert "human-progress" not in calls


def test_quiet_chat_without_activity_flag_writes_no_events(chat_entrypoint):
    code, out, err, _calls = chat_entrypoint(
        ["clover", "chat", "--oneshot", "-Q", "-q", "run the tests"]
    )

    assert code == 0
    assert _events(err) == []
    assert out.strip() == "all green"


def test_human_single_query_tees_activity_next_to_renderers(monkeypatch):
    """Non -Q ``chat -q`` keeps its human renderer AND emits the stream."""
    import cli as cli_mod
    from clover_cli.activity_events import ActivityEventWriter, attach_to_agent

    seen = []
    agent = SimpleNamespace(
        tool_progress_callback=lambda *a, **k: seen.append("human"),
        interim_assistant_callback=None,
        model_fallback_callback=None,
    )
    err = io.StringIO()
    writer = ActivityEventWriter(err)
    attach_to_agent(agent, writer, chain=True)
    attach_to_agent(agent, writer, chain=True)  # idempotent across re-inits
    writer.start("m")
    writer.start("m")
    agent.tool_progress_callback("tool.started", "terminal", "ls", {"command": "ls"})

    assert seen == ["human"]
    kinds = [e["event"] for e in _events(err.getvalue())]
    assert kinds == ["start", "tool.started"]

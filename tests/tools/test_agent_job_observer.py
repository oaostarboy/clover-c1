"""External agent CLI workers registered through terminal(agent_job=...).

Real subprocesses (``sys.executable`` scripts — portable, no POSIX-only
liveness) run through the real path:

    terminal_tool(background=True, agent_job=...)
      -> process_registry.spawn_local           (real Popen + reader thread)
      -> ProcessSession.agent_job observer       (_emit_output / _move_to_finished)
      -> activity sink bound for this "turn"    (DelegationActivityPublisher)
      -> Telegram-shaped adapter payloads

The fake workers emit genuine ``claude -p --output-format stream-json`` and
``clover -z --activity-events`` wire shapes.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import sys
import textwrap
import time

import pytest

import tools.terminal_tool as terminal_tool
from agent.delegation_activity import bind_activity_sink
from tests.gateway.test_delegation_activity import (
    FakeClock,
    FakeTelegramAdapter,
    _make_publisher,
)
from tools.agent_job_observer import AgentJobObserver, register_agent_job, validate_agent_job_spec
from tools.process_registry import process_registry

SECRET = "sk-ant-api03-" + "B" * 48


def _config(cwd):
    return {
        "env_type": "local", "cwd": str(cwd), "timeout": 5, "host_cwd": None,
        "modal_mode": "auto", "docker_image": "", "singularity_image": "",
        "modal_image": "", "daytona_image": "",
    }


@pytest.fixture
def hermetic_terminal(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": SimpleNamespace(env={})})
    monkeypatch.setattr(terminal_tool, "_last_activity", {"default": 0})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _config(tmp_path))
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_resolve_container_task_id", lambda v: v or "default")
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda c, e, **k: {"approved": True})
    return tmp_path


CLAUDE_WORKER = textwrap.dedent(f"""
    import json, sys, time
    def emit(o):
        print(json.dumps(o), flush=True); time.sleep(0.3)
    emit({{"type": "system", "subtype": "init", "model": "claude-opus-5-5"}})
    emit({{"type": "assistant", "message": {{"content": [
        {{"type": "thinking", "thinking": "PRIVATE-THOUGHT about secrets"}},
        {{"type": "text", "text": "Reading the scheduler lock code."}}]}}}})
    emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "tu1",
        "name": "Bash", "input": {{"command": "curl -H 'Authorization: Bearer {SECRET}' https://x.test"}}}}]}}}})
    emit({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "tu1",
        "is_error": False, "content": "RAW-TOOL-OUTPUT {SECRET}"}}]}}}})
    print("warning: plain text line {{ not json", flush=True)
    emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "tu2",
        "name": "Read", "input": {{"file_path": "cron/scheduler.py"}}}}]}}}})
    emit({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "tu2",
        "is_error": True, "content": "boom"}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": False,
        "result": "Found the lock race in cron/scheduler.py."}})
""")

CLOVER_WORKER = textwrap.dedent("""
    import json, sys, time
    def ev(**k):
        k["clover_activity"] = 1
        sys.stderr.write(json.dumps(k) + "\\n"); sys.stderr.flush(); time.sleep(0.3)
    ev(event="start", model="luna-large")
    ev(event="tool.started", tool="terminal", summary="pytest tests/cron -q")
    ev(event="tool.completed", tool="terminal", duration=1.5, is_error=False)
    ev(event="note", text="Cron catchup skips DST jobs; drafting a fix.")
    ev(event="reasoning", text="PRIVATE-REASONING must be ignored")
    sys.stdout.write("FINAL ANSWER PLAIN TEXT\\n"); sys.stdout.flush()
    ev(event="result", status="completed", text="Fixed DST catchup in cron/jobs.py.")
""")


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def _spawn(command, spec):
    return json.loads(
        terminal_tool.terminal_tool(command=command, background=True, agent_job=spec)
    )


async def _wait_finished(pub, group_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pub.drain()
        if pub.tracker.group_finished(group_id):
            await pub.drain()
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"group {group_id} never finished: {pub.tracker.snapshot(group_id)}")


@pytest.mark.asyncio
async def test_two_concurrent_external_workers_attributed_and_redacted(hermetic_terminal):
    tmp = hermetic_terminal
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    ctx = contextvars.copy_context()
    ctx.run(bind_activity_sink, pub)  # what the gateway turn does in run_sync

    r1 = ctx.run(_spawn, _script(tmp, "claude_worker.py", CLAUDE_WORKER),
                 {"title": "Audit cron locking", "model": "claude-opus-5-5",
                  "parser": "claude-stream-json"})
    r2 = ctx.run(_spawn, _script(tmp, "luna_worker.py", CLOVER_WORKER),
                 {"title": "Fix DST catchup", "model": "", "parser": "clover-activity"})
    assert r1["agent_job"]["observed"] is True and r2["agent_job"]["observed"] is True
    group_id = pub._jobs_group
    await _wait_finished(pub, group_id)

    everything = "\n".join(
        [c["content"] for c in adapter.status_calls] + [s["content"] for s in adapter.sends]
    )
    # Tool calls are visible and attributed by title + model.
    assert "Bash" in everything and "Read" in everything and "terminal" in everything
    assert any("**Audit cron locking**\n> *Opus 5.5" in c["content"] and "Bash" in c["content"]
               for c in adapter.status_calls)
    assert any("**Fix DST catchup**\n> *luna-large" in c["content"] for c in adapter.status_calls)
    # Public notes surface; private thinking, tool output, stray stdout never do.
    assert "Reading the scheduler lock code." in everything
    assert "Cron catchup skips DST jobs" in everything
    for forbidden in ("PRIVATE-THOUGHT", "PRIVATE-REASONING", "RAW-TOOL-OUTPUT",
                      "FINAL ANSWER PLAIN TEXT", "plain text line", SECRET):
        assert forbidden not in everything
    # Outcomes: Read failed, Bash ok; both workers reported done once.
    snap = {s["title"]: s for s in pub.tracker.snapshot(group_id)}
    assert snap["Audit cron locking"]["tools_ok"] == 1
    assert snap["Audit cron locking"]["tools_failed"] == 1
    assert snap["Fix DST catchup"]["model"] == "luna-large"
    assert all(s["state"] == "completed" for s in snap.values())
    # Both results arrive once, together, in one summary at the bottom.
    assert len(adapter.sends) == 1
    final = adapter.summary()
    assert "Found the lock race" in final and "Fixed DST catchup" in final
    assert final.startswith("✅ 2 subagents ·") and "🛠 3 tool calls ·" in final
    # One live card for the job group, removed once the summary lands.
    assert {c["key"] for c in adapter.status_calls} == {f"delegation:{group_id}"}
    assert adapter.deleted
    await pub.aclose()


@pytest.mark.asyncio
async def test_cancelled_and_failing_external_workers(hermetic_terminal):
    tmp = hermetic_terminal
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    ctx = contextvars.copy_context()
    ctx.run(bind_activity_sink, pub)
    sleeper = _script(tmp, "sleeper.py", "import time\nprint('booting', flush=True)\ntime.sleep(60)\n")
    failing = _script(tmp, "failing.py", "import sys\nprint('oops', flush=True)\nsys.exit(3)\n")
    r1 = ctx.run(_spawn, sleeper, {"title": "Long Codex job", "parser": "none"})
    r2 = ctx.run(_spawn, failing, {"title": "Broken worker", "parser": "none"})
    assert r1["agent_job"]["visibility"] == "lifecycle"
    await asyncio.sleep(0.5)
    await pub.drain()
    live = adapter.card()
    assert "Long Codex job" in live
    assert "no tool detail" in live
    assert "booting" not in live  # raw output is never shown

    process_registry.kill_process(r1["session_id"])
    await _wait_finished(pub, pub._jobs_group)
    snap = {s["title"]: s for s in pub.tracker.snapshot(pub._jobs_group)}
    assert snap["Long Codex job"]["state"] == "cancelled"
    assert snap["Broken worker"]["state"] == "failed"
    assert "exited with code 3" in snap["Broken worker"]["reason"]
    alerts = "\n".join(s["content"] for s in adapter.sends)
    assert "Long Codex job" in alerts and "stopped" in alerts
    assert "Broken worker" in alerts and "failed" in alerts
    assert "oops" not in alerts
    await pub.aclose()


@pytest.mark.asyncio
async def test_external_jobs_never_leak_across_chats(hermetic_terminal):
    tmp = hermetic_terminal
    adapter_a, adapter_b = FakeTelegramAdapter(), FakeTelegramAdapter()
    pub_a = _make_publisher(adapter_a, chat_id="chat-A", metadata={"thread_id": "1"})
    pub_b = _make_publisher(adapter_b, chat_id="chat-B", metadata={"thread_id": "2"})
    ctx_a, ctx_b = contextvars.copy_context(), contextvars.copy_context()
    ctx_a.run(bind_activity_sink, pub_a)
    ctx_b.run(bind_activity_sink, pub_b)
    quick = "import time\nprint('x', flush=True)\ntime.sleep(0.2)\n"
    ctx_a.run(_spawn, _script(tmp, "a.py", quick), {"title": "Alpha job", "parser": "none"})
    ctx_b.run(_spawn, _script(tmp, "b.py", quick), {"title": "Beta job", "parser": "none"})
    await _wait_finished(pub_a, pub_a._jobs_group)
    await _wait_finished(pub_b, pub_b._jobs_group)
    text_a = "\n".join(c["content"] for c in adapter_a.status_calls + adapter_a.sends)
    text_b = "\n".join(c["content"] for c in adapter_b.status_calls + adapter_b.sends)
    assert "Alpha job" in text_a and "Beta job" not in text_a
    assert "Beta job" in text_b and "Alpha job" not in text_b
    assert {c["chat_id"] for c in adapter_a.status_calls + adapter_a.sends} == {"chat-A"}
    assert all(c["metadata"] == {"thread_id": "2"} for c in adapter_b.status_calls)
    # Distinct card keys per turn publisher: a later turn can never edit this card.
    assert pub_a._jobs_group != pub_b._jobs_group
    await pub_a.aclose()
    await pub_b.aclose()


def test_unregistered_and_unobserved_processes_are_untouched(hermetic_terminal):
    tmp = hermetic_terminal
    plain = json.loads(terminal_tool.terminal_tool(
        command=_script(tmp, "plain.py", "print('hi')\n"), background=True))
    assert "agent_job" not in plain
    assert process_registry.get(plain["session_id"]).agent_job is None
    # Registration without a live activity surface (CLI, display off) is
    # reported honestly and attaches nothing.
    res = json.loads(terminal_tool.terminal_tool(
        command=_script(tmp, "plain2.py", "print('hi')\n"), background=True,
        agent_job={"title": "No surface", "model": "", "parser": "none"}))
    assert res["agent_job"]["observed"] is False
    assert process_registry.get(res["session_id"]).agent_job is None


def test_agent_job_argument_validation():
    handler = terminal_tool._handle_terminal
    err = json.loads(handler({"command": "echo", "agent_job": {"title": "x"}}))
    assert "background=true" in err["error"]
    err = json.loads(handler({"command": "echo", "background": True,
                              "agent_job": {"title": "x", "parser": "rot13"}}))
    assert "parser" in err["error"]
    err = json.loads(handler({"command": "echo", "background": True, "agent_job": {}}))
    assert "title" in err["error"]
    spec, error = validate_agent_job_spec({"title": " T ", "parser": "Claude-Stream-JSON"})
    assert error is None and spec == {"title": "T", "model": "", "parser": "claude-stream-json"}


class _Sink:
    def __init__(self):
        self.events = []

    def observe(self, event_type, tool_name=None, preview=None, args=None, **kw):
        self.events.append((event_type, tool_name, preview, args, kw))

    def external_job_identity(self):
        return "jobs_test", 0


def test_parser_is_bounded_and_fails_safe_on_malformed_and_private_input():
    sink = _Sink()
    obs = AgentJobObserver(session_id="proc_x", sink=sink, group_id="g", index=0,
                           title="T", parser="claude-stream-json")
    obs.feed('{"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "SECRET-PLAN"},'
             ' {"type": "redacted_thinking", "data": "zzz"}]}}\n')
    obs.feed("{not json at all\n")
    obs.feed('["a", "list"]\n')
    obs.feed('{"type": "assistant", "message": {"content": "not-a-list"}}\n')
    obs.feed('{"type": "assistant", "message": {"content": [7, null]}}\n')
    # An oversized line (e.g. a tool_result with a whole file) is dropped, and
    # parsing resumes on the next line — split across chunks on purpose.
    obs.feed('{"type": "user", "x": "' + "A" * 600_000)
    obs.feed("B" * 600_000 + '"}\n{"type": "assistant", "message": {"content": '
             '[{"type": "text", "text": "Next step."}]}}\n')
    kinds = [e[0] for e in sink.events]
    assert "subagent.thinking" in kinds
    notes = [e[2] for e in sink.events if e[0] == "subagent.thinking"]
    assert notes == ["Next step."]
    assert obs.malformed_lines >= 1 and obs.dropped_lines == 1
    assert "SECRET-PLAN" not in repr(sink.events)
    # finish is idempotent; exit maps to lifecycle states.
    obs.finish(0, "exited")
    obs.finish(1, "exited")
    completes = [e for e in sink.events if e[0] == "subagent.complete"]
    assert len(completes) == 1 and completes[0][4]["status"] == "completed"


def test_lifecycle_parser_never_reads_output():
    sink = _Sink()
    obs = AgentJobObserver(session_id="proc_y", sink=sink, group_id="g", index=0,
                           title="Plain", parser="none")
    obs.feed('{"type": "assistant", "message": {"content": [{"type": "text", "text": "HIDDEN"}]}}\n')
    assert sink.events == []
    assert obs.liveness()["registered"] is True and obs.liveness()["external"] is True
    obs.finish(None, "killed")
    assert sink.events[-1][4]["status"] == "interrupted"
    assert obs.liveness()["registered"] is False


@pytest.mark.asyncio
async def test_lifecycle_job_heartbeat_reports_output_activity_truthfully():
    from gateway.delegation_activity import _combined_liveness_probe

    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter, clock=clock, probe=lambda sid: live[sid]())
    obs_box = {}

    class _Session:
        id = "proc_hb"
        output_buffer = ""
        exited = False
        exit_code = None
        completion_reason = "exited"

        def __init__(self):
            import threading
            self._lock = threading.Lock()
            self.agent_job = None

    session = _Session()
    register_agent_job(session, {"title": "Quiet Codex", "model": "gpt-5.5", "parser": "none"}, sink=pub)
    obs_box["o"] = session.agent_job
    live = {"proc_hb": lambda: obs_box["o"].liveness()}
    obs_box["o"].feed("some output\n")
    clock.advance(61)
    await pub.heartbeat_tick()
    snap = pub.tracker.snapshot(pub._jobs_group)[0]
    assert snap["state"] == "running" and "output" in snap["reason"]
    # The real probe routes proc_ ids to the registry-backed job liveness.
    assert _combined_liveness_probe("proc_does_not_exist").get("registered") is False
    await pub.aclose()


def test_clover_activity_writer_round_trips_through_parser():
    import io

    from clover_cli.activity_events import ActivityEventWriter

    buf = io.StringIO()
    writer = ActivityEventWriter(buf)
    writer.start("luna-large")
    writer.tool_progress_callback("tool.started", "terminal", None,
                                  {"command": f"export OPENAI_API_KEY={SECRET} && pytest -q"})
    writer.tool_progress_callback("reasoning.available", "_thinking", "PRIVATE", None)
    writer.tool_progress_callback("_thinking", "PRIVATE-SCRATCH")
    writer.tool_progress_callback("tool.completed", "terminal", None, None,
                                  duration=2.0, is_error=False, result="RAW OUTPUT")
    writer.interim_callback("<think>PRIVATE-BLOCK</think>Running the cron suite now.")
    writer.result("All green.\nDetails follow...", "completed")
    raw = buf.getvalue()
    for forbidden in (SECRET, "PRIVATE", "RAW OUTPUT", "Details follow"):
        assert forbidden not in raw
    lines = [json.loads(l) for l in raw.splitlines()]
    assert all(l["clover_activity"] == 1 for l in lines)
    assert [l["event"] for l in lines] == ["start", "tool.started", "tool.completed", "note", "result"]

    sink = _Sink()
    obs = AgentJobObserver(session_id="proc_z", sink=sink, group_id="g", index=0,
                           title="Luna", parser="clover-activity")
    obs.feed(raw)
    obs.finish(0, "exited")
    kinds = [e[0] for e in sink.events]
    assert kinds.count("subagent.tool") == 1 and kinds.count("subagent.tool_done") == 1
    assert sink.events[-1][4]["summary"] == "All green."


def test_writer_survives_a_closed_pipe():
    from clover_cli.activity_events import ActivityEventWriter

    class _Closed:
        def write(self, _):
            raise BrokenPipeError

        def flush(self):
            pass

    writer = ActivityEventWriter(_Closed())
    writer.start("m")  # must not raise
    writer.interim_callback("note")


def test_activity_events_flag_is_parsed():
    from clover_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()
    parsed = parser.parse_args(["-z", "do it", "--activity-events"])
    assert parsed.activity_events is True
    assert parser.parse_args(["-z", "do it"]).activity_events is False



def test_turn_cap_is_unfinished_not_failed():
    """claude -p hitting --max-turns exits 1 with subtype error_max_turns:
    the card shows it as unfinished (⏳), never as a crash."""
    import json
    from tools.agent_job_observer import AgentJobObserver

    seen = []

    class Sink:
        def observe(self, event_type, *a, **kw):
            seen.append((event_type, kw))

    obs = AgentJobObserver(session_id="proc_x", sink=Sink(), group_id="jobs_x", index=0,
                           title="Big task", model="claude-sonnet-5", parser="claude-stream-json")
    obs.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                         "result": ""}) + "\n")
    obs.finish(1)
    done = [kw for ev, kw in seen if ev == "subagent.complete"]
    assert done and done[-1]["status"] == "incomplete"


def _clocked_job(pub, clock, *, parser="claude-stream-json", title="Queued worker"):
    group_id, index = pub.external_job_identity()
    obs = AgentJobObserver(session_id="proc_q", sink=pub, group_id=group_id, index=index,
                           title=title, parser=parser, clock=clock)
    obs.start()
    return obs


@pytest.mark.asyncio
async def test_queued_job_with_no_output_is_waiting_never_stuck():
    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    obs_box = {}
    pub = _make_publisher(adapter, clock=clock, probe=lambda sid: obs_box["o"].liveness())
    obs_box["o"] = _clocked_job(pub, clock)
    assert obs_box["o"].liveness()["has_output"] is False
    for _ in range(15):  # 15 minutes of queue silence
        clock.advance(60)
        await pub.heartbeat_tick()
    snap = pub.tracker.snapshot(pub._jobs_group)[0]
    assert snap["state"] == "waiting"
    assert snap["reason"] == "queued (waiting to start)"
    assert adapter.sends == []
    await pub.aclose()


@pytest.mark.asyncio
async def test_job_that_produced_output_then_went_quiet_alerts_once_with_new_wording():
    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    obs_box = {}
    pub = _make_publisher(adapter, clock=clock, probe=lambda sid: obs_box["o"].liveness())
    obs_box["o"] = _clocked_job(pub, clock)
    obs_box["o"].feed('{"type": "system", "subtype": "init"}\n')
    clock.advance(660)  # 11 minutes of silence after real output
    await pub.heartbeat_tick()
    clock.advance(60)
    await pub.heartbeat_tick()
    assert pub.tracker.snapshot(pub._jobs_group)[0]["state"] == "blocked"
    assert len(adapter.sends) == 1
    text = adapter.sends[0]["content"]
    assert "Queued worker has been quiet for 11m." in text
    assert "still running; I'll keep watching" in text
    assert "stuck" not in text
    await pub.aclose()


@pytest.mark.asyncio
async def test_stream_heartbeat_lines_count_as_activity():
    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    obs_box = {}
    pub = _make_publisher(adapter, clock=clock, probe=lambda sid: obs_box["o"].liveness())
    obs = obs_box["o"] = _clocked_job(pub, clock)
    obs.feed('{"type": "system", "subtype": "init"}\n')
    line = ('{"type":"tool_progress","tool_use_id":"toolu_1","tool_name":"Bash",'
            '"elapsed_time_seconds":30,"heartbeat":true,"session_id":"s"}\n')
    for _ in range(20):  # a 20-minute tool that keeps emitting heartbeats
        clock.advance(60)
        obs.feed(line)
        await pub.heartbeat_tick()
    snap = pub.tracker.snapshot(pub._jobs_group)[0]
    assert snap["state"] == "running"
    assert obs.liveness()["seconds_since_activity"] < 1
    assert adapter.sends == []
    await pub.aclose()

"""Real `clover --activity-events chat --oneshot` worker -> card, end to end.

    terminal_tool(background=True, agent_job={parser: "clover-activity"})
      -> process_registry.spawn_local   (real Popen; stdout+stderr MERGED)
      -> this checkout's real CLI, non-quiet chat single-query path, against
         an in-process OpenAI-compatible mock (no network, no credentials)
      -> AgentJobObserver (clover-activity parser)
      -> DelegationActivityPublisher / tracker -> Telegram-shaped card

Non-quiet means the worker's human output (banner, "Query:", response box,
tool previews) shares the pipe with the JSONL. The card must still get every
tool call and the result. On the C1.2 build this path wrote no JSONL at all
(``cmd_chat`` never forwarded --activity-events), so the card showed only
"thinking…" for the whole run.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import shlex
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path

import pytest

import tools.terminal_tool as terminal_tool
from agent.delegation_activity import bind_activity_sink
from tests.clover_cli.test_oneshot_activity_events import _handler
from tests.gateway.test_delegation_activity import FakeTelegramAdapter, _make_publisher
from tests.tools.test_agent_job_observer import hermetic_terminal  # noqa: F401  (fixture)
from tools.agent_job_observer import AgentJobObserver

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def mock_model_home(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("smoke\n", encoding="utf-8")
    queue = [
        {"content": "Checking the notes file.", "tool_calls": [{"id": "call_1", "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps({"command": f"cat {notes}"})}}]},
        {"content": "Now counting lines.", "tool_calls": [{"id": "call_2", "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps({"command": f"wc -l {notes}"})}}]},
        {"content": "The notes file says smoke.", "tool_calls": None},
    ]
    srv = HTTPServer(("127.0.0.1", 0), _handler(queue))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home = tmp_path / "worker_home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n"
        f"  base_url: http://127.0.0.1:{srv.server_address[1]}/v1\n"
        "  default: mock-luna\n  api_key: local-mock-key\n  context_length: 128000\n",
        encoding="utf-8",
    )
    yield home
    srv.shutdown()


async def _wait_finished(pub, group_id, timeout=150.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pub.drain()
        if pub.tracker.group_finished(group_id):
            await pub.drain()
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"worker never finished: {pub.tracker.snapshot(group_id)}")


@pytest.mark.asyncio
async def test_non_quiet_chat_oneshot_worker_tools_reach_the_card(
    hermetic_terminal, mock_model_home  # noqa: F811
):
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    ctx = contextvars.copy_context()
    ctx.run(bind_activity_sink, pub)
    command = " ".join([
        f"CLOVER_HOME={shlex.quote(str(mock_model_home))}",
        shlex.quote(sys.executable), shlex.quote(str(REPO / "clover")),
        "--activity-events", "chat", "--oneshot",
        "-q", shlex.quote("Read notes.txt"), "-t", "terminal",
    ])
    result = json.loads(ctx.run(
        terminal_tool.terminal_tool, command=command, background=True,
        agent_job={"title": "Read the notes", "model": "mock-luna", "parser": "clover-activity"},
    ))
    assert result["agent_job"]["observed"] is True, result
    group_id = pub._jobs_group
    await _wait_finished(pub, group_id)

    snap = pub.tracker.snapshot(group_id)[0]
    assert snap["state"] == "completed", snap
    assert snap["tools_ok"] == 2, snap
    shown = "\n".join(c["content"] for c in adapter.status_calls)
    assert "cat" in shown and "wc -l" in shown, shown
    assert "The notes file says smoke." in adapter.summary()
    # Human chatter on the merged pipe never reaches the card.
    for leaked in ("Query:", "Resume this session", "╭─"):
        assert leaked not in shown and leaked not in adapter.summary()
    await pub.aclose()


class _Sink:
    def __init__(self) -> None:
        from agent.delegation_activity import DelegationActivityTracker

        self.tracker = DelegationActivityTracker()

    def observe(self, *args, **kw):
        self.tracker.observe(*args, **kw)


def test_jsonl_glued_to_unterminated_human_output_is_still_read():
    """Non-quiet workers share one pipe: a JSONL record can land right after a
    human write that had no trailing newline (streamed text, spinner frame).
    The record is still a whole, versioned line; recover it."""
    sink = _Sink()
    obs = AgentJobObserver(session_id="w", sink=sink, group_id="g", index=0,
                           title="Glued", parser="clover-activity")
    obs.start()
    import io

    from clover_cli.activity_events import ActivityEventWriter

    def ev(call):  # the real writer's wire line
        buf = io.StringIO()
        call(ActivityEventWriter(buf))
        return buf.getvalue()

    obs.feed("  ┊ ⌨️ preparing terminal…" + ev(lambda w: w.tool_progress_callback(
        "tool.started", "terminal", "git log", {"command": "git log"}, tool_call_id="c1")))
    obs.feed("\rThe answer so far" + ev(lambda w: w.tool_progress_callback(
        "tool.completed", "terminal", None, None, duration=0.2, is_error=False,
        tool_call_id="c1")))
    # Non-versioned / foreign JSON after text stays ignored.
    obs.feed('noise {"event": "tool.started", "tool": "x"}\n')
    snap = sink.tracker.snapshot("g")[0]
    assert snap["tools_ok"] == 1 and snap["tools_failed"] == 0, snap
    assert obs.malformed_lines == 0

"""Outside agent jobs (terminal ``agent_job``) show on the unified subagent card:
they join the chat's running card or get their own, and never get the old 🔀
roster card.  Classic style is unchanged."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent import ack_checklist as ac
from gateway import delegation_activity as da
from tests.gateway.test_ack_card_follow import _Chat, _dispatch, _opener_card, _settle
from tests.gateway.test_delegation_activity import FakeClock, _make_publisher
from tools.agent_job_observer import register_agent_job

pytestmark = pytest.mark.unified_card

CLAUDE = "claude-stream-json"


class _TgChat(_Chat):
    """Telegram-shaped chat that records any 🔀 roster-card post."""

    def __init__(self) -> None:
        super().__init__()
        self.platform = SimpleNamespace(value="telegram")
        self.status_calls: List[Dict[str, Any]] = []

    async def send_or_update_status(self, chat_id, status_key, content, *, metadata=None):
        self.status_calls.append({"key": status_key, "content": content})
        from gateway.platforms.base import SendResult

        return SendResult(success=True, message_id="roster1")


class _Session:
    id = "proc_job1"
    output_buffer = ""
    exited = False
    exit_code = None
    completion_reason = "exited"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.agent_job = None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    for reg in (da._ACK_EDITORS, da._LIVE, da._LAST_OUT, da._LAST_PLAIN, da._FOLLOW_TIMERS):
        reg.clear()
    monkeypatch.setattr(da, "FOLLOW_DEBOUNCE_SECONDS", 0.0)
    yield
    ac.reset_for_tests()
    for reg in (da._ACK_EDITORS, da._LIVE, da._LAST_OUT, da._LAST_PLAIN, da._FOLLOW_TIMERS):
        reg.clear()


def _line(obj: Dict[str, Any]) -> str:
    return json.dumps(obj) + "\n"


def _tool_use(call_id: str, name: str, **inp: Any) -> str:
    return _line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": inp}]}})


def _tool_result(call_id: str) -> str:
    return _line({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": "x"}]}})


async def _start_job(chat, title="Audit cron locking", model="gpt-6.1-sol-900k", parser=CLAUDE, sid="proc_job1"):
    pub = _make_publisher(chat, chat_id="chat", clock=FakeClock())
    session = _Session()
    session.id = sid
    register_agent_job(session, {"title": title, "model": model, "parser": parser}, sink=pub)
    await _settle()
    return pub, session.agent_job


def _card_text(chat) -> str:
    editors = list(da._ACK_EDITORS.values())
    assert editors, "a unified card was posted"
    return editors[-1]._render()


@pytest.mark.asyncio
async def test_an_agent_job_joins_the_running_card_and_posts_no_roster_card():
    chat = _TgChat()
    first = _opener_card("dj1", "shipping the card", "ship it", "on it")
    await _dispatch(chat, first)
    host_editor = da._ACK_EDITORS[id(first)]
    pub, job = await _start_job(chat)
    assert chat.status_calls == [] and pub.tracker.group_ids() == [], "no 🔀 roster card"
    assert len(da._ACK_EDITORS) == 1, "no second card message"
    assert len(first.parts) == 2 and first.parts[1].card is first
    job.feed(_tool_use("c1", "Read", file_path="/home/x/a.py"))
    job.feed(_tool_result("c1"))
    job.feed(_tool_use("c2", "Bash", command="pytest -q"))
    host_editor._min_interval = 0.0
    host_editor._last_attempt = float("-inf")
    text = host_editor._render()
    head = text.splitlines()[0]
    assert "**2 jobs**" in head and "0 of 2 done" in head and "shipping" not in head
    assert "\u25a0 **shipping the card**" in text
    # a long name puts its stats on the next indented line
    assert "\u25a0 **Audit cron locking**\n> \u2003\u2003GPT-6.1 Sol \u00b7 \U0001f6e0 2 calls \u00b7 \u23f1 " in text
    assert "*now: pytest -q*" in text and "*before: Read a.py*" in text
    job.finish(0)
    assert first.parts[1].checklist.state == ac.DONE and not first.finished, "the host part still runs"
    await pub.aclose()


@pytest.mark.asyncio
async def test_an_agent_job_alone_gets_its_own_unified_card():
    chat = _TgChat()
    pub, job = await _start_job(chat)
    assert chat.status_calls == []
    assert len(chat.sends) == 1, "one card message"
    text = chat.sends[0]["text"]
    assert text.startswith("**> \U0001f500 **Audit cron locking**\n> *tap to watch")
    assert "tap to watch" in text and "Goal:" not in text
    job.feed(_tool_use("c1", "Bash", command="cd /repo && FOO=1 make test"))
    card = list(da._ACK_EDITORS.values())[0].card
    cl = card.parts[0].checklist
    assert cl.model == "GPT-6.1 Sol" and cl.calls == 1
    assert "*now: make test*" in card.render()
    job.feed(_line({"type": "result", "result": "Plain summary: Locking is fine.", "is_error": False}))
    job.finish(0)
    assert cl.state == ac.DONE and cl.summary == "Locking is fine."
    assert "Locking is fine." in card.render() and card.finished
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_failed_agent_job_gets_the_failure_mark():
    chat = _TgChat()
    pub, job = await _start_job(chat)
    job.feed(_tool_use("c1", "Bash", command="make"))
    job.finish(2)
    card = list(da._ACK_EDITORS.values())[0].card
    text = card.render()
    assert card.parts[0].checklist.state == ac.FAILED
    assert text.splitlines()[0] == "\U0001f342 **Audit cron locking** \u00b7 failed"
    assert "exited with code 2" in text
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_failed_job_in_a_joined_card_keeps_the_failure_mark_on_its_name():
    chat = _TgChat()
    first = _opener_card("dj1", "shipping the card", "ship it", "on it")
    await _dispatch(chat, first)
    pub, job = await _start_job(chat)
    job.finish(1)
    text = first.render()
    assert "\U0001f342 **Audit cron locking** \u00b7 failed" in text
    assert "\u25a0 **shipping the card**" in text
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_lifecycle_only_job_shows_running_then_done():
    chat = _TgChat()
    pub, job = await _start_job(chat, parser="none", model="")
    card = list(da._ACK_EDITORS.values())[0].card
    text = card.render()
    assert "*running*" in text and "now:" not in text and "calls" not in text
    assert "\u23f1" in text
    job.feed("some output\n")
    job.finish(0)
    done = card.render()
    assert card.parts[0].checklist.state == ac.DONE and card.finished
    assert done.splitlines()[0].endswith("\u00b7 done") and "*running*" not in done
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_finished_card_is_not_reopened_by_a_job_it_gets_its_own():
    chat = _TgChat()
    first = _opener_card("dj1", "shipping the card", "ship it", "on it")
    await _dispatch(chat, first)
    ac.on_child_event("subagent.complete", delegation_id="dj1", status="completed", summary="ok")
    assert first.finished
    pub, job = await _start_job(chat)
    assert len(first.parts) == 1 and len(da._ACK_EDITORS) == 2
    await pub.aclose()


@pytest.mark.asyncio
async def test_classic_style_still_posts_the_roster_card_for_an_agent_job():
    ac.set_style_for_tests("classic")
    chat = _TgChat()
    pub, job = await _start_job(chat)
    job.feed(_tool_use("c1", "Bash", command="make"))
    await pub.drain()
    assert da._ACK_EDITORS == {} and ac._PARTS == {}
    assert pub.tracker.group_ids() != [], "the 🔀 roster card path is untouched"
    assert chat.status_calls or chat.sends
    await pub.aclose()

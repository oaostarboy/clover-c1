"""Native accepted handoffs must share a live Telegram card across turns."""
from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from agent import router_backstop as rb
from agent.delegation_activity import bind_activity_sink
from gateway import delegation_activity as da
from gateway.config import PlatformConfig
from gateway.session_context import set_session_vars, clear_session_vars
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_delegation_activity import _turn_runner
from tests.tools.test_delegate_strict_background import (
    _clean_registry, _checkpoint_opted_in, _root, harness,
)
import tools.delegate_tool as dt
from tools import delegation_inflight

pytestmark = pytest.mark.unified_card


@pytest.fixture(autouse=True)
def cards():
    ac.reset_for_tests()
    ac.set_style_for_tests(None)
    assert ac.card_style() == "unified"  # Production default, not a forced style.
    bind_activity_sink(None)
    yield
    bind_activity_sink(None)
    for timer in da._FOLLOW_TIMERS.values():
        timer.cancel()
    da._FOLLOW_TIMERS.clear()
    for editor in da._ACK_EDITORS.values():
        editor.gone = True
        if editor._task:
            editor._task.cancel()
        if editor._repost_timer:
            editor._repost_timer.cancel()
    ac.reset_for_tests()


class Wire:
    """Only Telegram's network boundary is fake; formatting/send/edit are real."""
    def __init__(self):
        self.adapter = TelegramAdapter(PlatformConfig(enabled=True, token="offline-test"))
        self.adapter._rich_send_disabled = True
        self.messages = {}
        self.events = []
        self.seq = 100
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=self.send)
        bot.edit_message_text = AsyncMock(side_effect=self.edit)
        bot.delete_message = AsyncMock(side_effect=self.delete)
        self.adapter._bot = bot

    async def send(self, **kw):
        await asyncio.sleep(0)  # Bot I/O yields in the real adapter, too.
        self.seq += 1
        self.messages[self.seq] = kw
        self.events.append(("send", self.seq, kw))
        return SimpleNamespace(message_id=self.seq)

    async def edit(self, **kw):
        await asyncio.sleep(0)
        mid = int(kw["message_id"])
        assert mid in self.messages, "late events must not edit deleted cards"
        self.messages[mid] = {**self.messages[mid], **kw}
        self.events.append(("edit", mid, kw))
        return SimpleNamespace(message_id=mid)

    async def delete(self, **kw):
        mid = int(kw["message_id"])
        assert str(self.messages[mid]["chat_id"]) == str(kw["chat_id"])
        self.messages.pop(mid)
        self.events.append(("delete", mid, kw))
        return True

    def live(self, chat="123", thread=None):
        return {mid: kw for mid, kw in self.messages.items()
                if str(kw["chat_id"]) == chat
                and kw.get("message_thread_id") == thread
                and "tap to watch" in kw["text"]}


def publisher(wire, chat="123", thread=None):
    return da.DelegationActivityPublisher(
        adapter=wire.adapter, chat_id=chat,
        metadata={"thread_id": thread} if thread else None,
        loop=asyncio.get_running_loop(), combined=True,
        auto_heartbeat=False, heartbeat_seconds=0, min_interval=0,
    )


async def settle():
    for _ in range(40):
        await asyncio.sleep(0)


@pytest.fixture
def native(harness, monkeypatch, _clean_registry):
    """Real admission, async registry, receipt and child-event relay; fake work."""
    jobs = {}
    releases = []
    build_child = dt._build_child_agent

    def build(**kw):
        child = build_child(**kw)
        child._subagent_id = f"native-child-{kw['parent_agent']._current_turn_id}-{kw['task_index']}"
        child.session_id = child._subagent_id
        child._progress_identity_ref = {"session_id": child.session_id}
        child.tool_progress_callback = dt._build_child_progress_callback(
            kw["task_index"], kw["goal"], kw["parent_agent"], kw["task_count"],
            subagent_id=child._subagent_id, depth=0, model=kw["parent_agent"].model,
            session_ref=child._progress_identity_ref,
        )
        return child

    def run(task_index, goal, child, parent_agent, **kw):
        job = jobs[parent_agent._current_turn_id]
        cb = child.tool_progress_callback
        cb("subagent.start")
        cb("tool.started", "read_file", "links.txt", {"path": "links.txt"})
        job["callbacks"][task_index] = cb
        job["started"][task_index].set()
        job["release"].wait(45)
        return {"task_index": task_index, "status": "completed",
                "summary": "Plain summary: Finished the offline check.", "api_calls": 1,
                "duration_seconds": 0.0, "model": "m", "exit_reason": "completed"}

    monkeypatch.setattr(dt, "_build_child_agent", build)
    monkeypatch.setattr(dt, "_run_single_child", run)
    monkeypatch.setattr(dt, "_get_max_async_children", lambda: 8)

    async def launch(pub, turn_id, *, say="", title="checking links", model="claude-sonnet-5-5", tasks=None):
        root = _root()
        root.platform = "telegram"
        root.model = model
        root._current_turn_id = turn_id
        root.tool_progress_callback = _turn_runner(pub).progress_callback
        release = threading.Event()
        releases.append(release)
        tasks = tasks or [{"goal": title, "title": title, "steps": ["inspect links", "verify result"]}]
        job = jobs[turn_id] = {"release": release, "callbacks": {},
                              "started": [threading.Event() for _ in tasks]}
        bind_activity_sink(pub)
        tokens = set_session_vars(platform="telegram", source="telegram", chat_id=pub._chat_id,
                                  thread_id=str((pub._metadata or {}).get("thread_id") or ""),
                                  session_key="agent:main:telegram:dm:" + pub._chat_id,
                                  session_id=root.session_id)
        try:
            result = json.loads(await asyncio.to_thread(
                dt.delegate_task, parent_agent=root,
                tasks=tasks,
                background=True, handoff={"work": title, "outcome": "a checked result", "say": say},
            ))
        finally:
            clear_session_vars(tokens)
        assert result["status"] == "dispatched", result
        for started in job["started"]:
            assert await asyncio.to_thread(started.wait, 3)
        receipts = fl.own_dispatch_receipts(root)
        assert len(receipts) == 1 and receipts[0].turn_id == turn_id
        reply = rb._ack_text(receipts, root, [])
        await pub._adapter.send(pub._chat_id, reply, metadata=pub._metadata)
        await settle()
        for editor in da._ACK_EDITORS.values():
            editor._min_interval = 0
        await asyncio.sleep(da.ACK_MIN_INTERVAL + 0.05)  # First finalize=True edit.
        callbacks = tuple(job["callbacks"][index] for index in range(len(tasks)))
        for cb in callbacks:
            cb.delegation_id = result["delegation_id"]
            cb.release = release
        callbacks[0].callbacks = callbacks
        return callbacks[0]

    yield launch
    for release in releases:
        release.set()
    # Publication belongs to this fixture too. Join before _clean_registry
    # clears the batch records, otherwise a released child races the next test.
    from tools import async_delegation as ad

    executor = ad._executor
    ad._reset_for_tests(drain_timeout=5)
    assert executor is None or all(not worker.is_alive() for worker in executor._threads)


@pytest.mark.asyncio
async def test_native_empty_say_handoffs_join_and_follow_after_160_turns(monkeypatch, native):
    # Real 1.5s debounce remains enabled. Repost throttle need not slow the test.
    monkeypatch.setattr(da, "ACK_REPOST_MIN_SECONDS", 0)
    wire = Wire()
    pubs = [publisher(wire)]
    try:
        first = await native(pubs[0], "deleg_poster", title="designing posters")
        original = next(iter(wire.live()))
        for turn in range(160):
            pubs.append(publisher(wire))
            await wire.adapter.send("123", f"ordinary final reply {turn}")
        pubs.append(publisher(wire))
        second = await native(pubs[-1], "deleg_links", title="checking links", model="gpt-5.5")
        assert len(wire.live()) == 1, "two active native handoffs must share ONE live card"
        assert len([e for e in da._ACK_EDITORS.values() if not e._closed]) == 1
        with da.status_send():
            await wire.adapter.send("123", "🛠 3 tool calls")
        reply = await wire.adapter.send("123", "how are you? I'm here.")
        await asyncio.sleep(1.65)  # Actual module timer -> follow_latest_message.
        assert len(wire.live()) == 1
        current = next(iter(wire.live()))
        assert current > int(reply.message_id)
        assert original not in wire.messages
        editor = next(e for e in da._ACK_EDITORS.values() if not e._closed)
        assert editor in da._LIVE[da._inbox_key(wire.adapter, "123")]
        assert {p.delegation_id for p in editor.card.parts} == {first.delegation_id, second.delegation_id}
        for did in (first.delegation_id, second.delegation_id):
            assert delegation_inflight.read_record(did)["card"]["message_id"] == str(current)
        first("tool.completed", "read_file", "links.txt")
        second("tool.started", "web_search", "check links", {"query": "check links"})
        await settle()
        assert int(editor._message_id) == current
        assert "designing posters" in editor._last_text and "checking links" in editor._last_text
        assert "Sonnet 5.5" in editor._last_text and "GPT-5.5" in editor._last_text
        assert "inspect links" in editor._last_text and "calls" in editor._last_text
        edits = [kw for kind, mid, kw in wire.events if kind == "edit" and mid == current]
        assert edits and all(kw.get("parse_mode") == "MarkdownV2" for kw in edits)
        # MarkdownV2's **> prefix is Telegram's expandable-quote delimiter,
        # not leaked bold markdown. Titles themselves must use formatted *...*.
        assert all("**" not in kw["text"].replace("**>", ">") for kw in edits)
        deletes = Counter(mid for kind, mid, _ in wire.events if kind == "delete")
        assert deletes[original] == 1 and max(deletes.values()) == 1
    finally:
        for pub in pubs:
            await pub.aclose()

@pytest.mark.asyncio
async def test_native_cards_do_not_join_or_follow_another_thread(monkeypatch, native):
    monkeypatch.setattr(da, "ACK_REPOST_MIN_SECONDS", 0)
    wire = Wire()
    pubs = [publisher(wire, thread="7"), publisher(wire, thread="8"),
            publisher(wire, chat="456"), publisher(wire)]
    try:
        first = await native(pubs[0], "deleg_thread7", say="checking thread seven")
        await native(pubs[1], "deleg_thread8", say="checking thread eight")
        await native(pubs[2], "deleg_chat456", say="checking a different chat")
        await native(pubs[3], "deleg_unthreaded", say="checking the main chat")
        assert len(wire.live(thread=7)) == len(wire.live(thread=8)) == len(wire.live(chat="456")) == 1
        seven = set(wire.live(thread=7))
        foreign = set(wire.live(thread=8)) | set(wire.live(chat="456")) | set(wire.live())
        reply = await wire.adapter.send("123", "reply in seven", metadata={"thread_id": "7"})
        await asyncio.sleep(1.65)
        assert next(iter(wire.live(thread=7))) > int(reply.message_id)
        assert seven.isdisjoint(wire.messages)
        assert foreign.issubset(wire.messages), "another chat/thread's IDs must never be deleted"
        first("subagent.complete", status="completed", summary="Plain summary: Thread seven ended.")
        da.start_completion_turn([first.delegation_id])
        await wire.adapter.send("123", "thread seven result", metadata={"thread_id": "7"})
        da.start_completion_turn(None)
        await asyncio.sleep(1.65)
        assert foreign.issubset(wire.messages), "completion in a topic must not move the main-chat card"
    finally:
        for pub in pubs:
            await pub.aclose()


@pytest.mark.asyncio
async def test_native_simultaneous_turn_placement_shares_one_card(native):
    wire = Wire()
    pubs = [publisher(wire), publisher(wire)]
    try:
        await asyncio.gather(native(pubs[0], "deleg_race1", say="first opener"),
                             native(pubs[1], "deleg_race2", say="second opener"))
        assert len(wire.live()) == 1, "separate turn publishers must serialize placement"
        assert len([e for e in da._ACK_EDITORS.values() if not e._closed]) == 1
    finally:
        for pub in pubs:
            await pub.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,report", [
    ("completed", None),
    ("completed", {"status": "blocked", "remaining": ["Needs an external approval"]}),
    ("interrupted", None),
])
async def test_native_ended_child_leaves_board_and_never_follows_again(monkeypatch, native, status, report):
    monkeypatch.setattr(da, "ACK_REPOST_MIN_SECONDS", 0)
    wire = Wire()
    pubs = [publisher(wire), publisher(wire)]
    try:
        first = await native(pubs[0], "ending", title="first work")
        second = await native(pubs[1], "remaining", say="I am on the next task.", title="second work")
        first("subagent.complete", status=status, summary="Plain summary: First check ended.", task_outcome=report)
        da.start_completion_turn([first.delegation_id])
        await wire.adapter.send("123", "Here is the first result.")
        da.start_completion_turn(None)
        await asyncio.sleep(1.65)
        editor = next(e for e in da._ACK_EDITORS.values() if not e._closed)
        assert "second work" in editor._last_text and "first work" not in editor._last_text
        assert len(wire.live()) == 1
        finished_ids = {mid for mid, kw in wire.messages.items() if "first work" in kw["text"]}
        assert len(finished_ids) == 1
        # Late producer activity for an ended child cannot re-add its section.
        first("tool.started", "terminal", "late event", {"command": "true"})
        second("tool.started", "web_search", "still running", {"query": "still running"})
        reply = await wire.adapter.send("123", "another ordinary reply")
        await asyncio.sleep(1.65)
        assert finished_ids.issubset(wire.messages)
        assert next(iter(wire.live())) > int(reply.message_id)
        assert "first work" not in editor._last_text
        # The already-armed real follow callback must not resurrect the last child.
        await wire.adapter.send("123", "last reply before completion")
        second("subagent.complete", status="completed", summary="Plain summary: Second check ended.")
        await settle()
        sends_before = sum(kind == "send" for kind, _, _ in wire.events)
        await asyncio.sleep(1.65)
        assert editor._closed and not wire.live()
        assert sum(kind == "send" for kind, _, _ in wire.events) == sends_before
    finally:
        da.start_completion_turn(None)
        for pub in pubs:
            await pub.aclose()


@pytest.mark.asyncio
async def test_native_other_profile_adapter_never_groups_or_deletes_cards(monkeypatch, native):
    monkeypatch.setattr(da, "ACK_REPOST_MIN_SECONDS", 0)
    wire, foreign = Wire(), Wire()
    pubs = [publisher(wire), publisher(foreign)]
    try:
        await native(pubs[0], "profile_a", say="first profile")
        await native(pubs[1], "profile_b", say="second profile")
        other_ids = set(foreign.live())
        before = list(foreign.events)
        await wire.adapter.send("123", "only in profile A")
        await asyncio.sleep(1.65)
        assert other_ids == set(foreign.live())
        assert all(kind != "delete" for kind, _, _ in foreign.events[len(before):])
        assert len([e for e in da._ACK_EDITORS.values() if not e._closed]) == 2
    finally:
        for pub in pubs:
            await pub.aclose()


"""Screenshot-shaped native two-child handoff, then an observed Sonnet CLI job.

Fake child work and Telegram network only; native admission/router/registry,
observer JSONL parsing, adapter formatting, placement and follow timers are real.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import pytest

from agent import ack_checklist as ac
from gateway import delegation_activity as da
from tests.gateway.test_agent_job_in_card import _Session, _tool_use, _tool_result
from tests.gateway.test_native_card_grouping import (
    Wire, publisher, settle, native, cards, harness, _clean_registry, _checkpoint_opted_in,
)
from tools import delegation_inflight
from tools.agent_job_observer import register_agent_job

pytestmark = pytest.mark.unified_card


@pytest.mark.asyncio
@pytest.mark.parametrize("say", ["", "I am checking this PC."], ids=["empty-say", "with-say"])
async def test_native_two_helpers_then_observed_sonnet_share_following_card(monkeypatch, native, say):
    # Do not bypass the real 1.5-second follow debounce; only remove repost rate limiting.
    monkeypatch.setattr(da, "ACK_REPOST_MIN_SECONDS", 0)
    wire = Wire()
    pubs = [publisher(wire)]
    session = _Session()
    session.id = "proc_mixed_sonnet"
    job: Any = None
    try:
        first = await native(pubs[0], "mixed_pc", say=say, title="checking this PC", tasks=[
            {"goal": "check hardware", "title": "check hardware", "steps": ["inspect hardware", "verify hardware"]},
            {"goal": "check software", "title": "check software", "steps": ["inspect software", "verify software"]},
        ])
        assert len(first.callbacks) == 2
        assert len(wire.live()) == 1
        original = next(iter(wire.live()))
        editor = next(e for e in da._ACK_EDITORS.values() if not e._closed)
        native_part = editor.card.parts[0]
        assert native_part.expected == 2 and len(native_part.checklist.kids) == 2
        # Observe initial native activity separately; later valid activity is
        # not proof that the initial model/tool events reached the part.
        native_initial = [{"model": kid.model, "calls": kid.calls,
                           "open": list(kid.open), "recent": list(kid.recent)}
                          for kid in native_part.checklist.kids]
        assert len({id(cb) for cb in first.callbacks}) == 2, "two distinct native child producers"
        assert "2 subagents" in editor._last_text and "0 of 2 done" in editor._last_text
        opener_text = wire.adapter.format_message(say) if say else ""
        opener_ids = {mid for mid, kw in wire.messages.items() if say and kw["text"] == opener_text}
        assert len(opener_ids) == bool(say)

        # Unrelated ordinary turns separate the old PC card from the later print task.
        for turn in range(3):
            pubs.append(publisher(wire))
            await wire.adapter.send("123", f"ordinary reply before print task {turn}")
        await asyncio.sleep(da.FOLLOW_DEBOUNCE_SECONDS + 0.15)
        assert len(wire.live()) == 1 and original not in wire.messages
        before_cli = next(iter(wire.live()))

        pubs.append(publisher(wire))
        registered = register_agent_job(session, {
            "title": "finding useful prints", "model": "claude-sonnet-5-5", "parser": "claude-stream-json",
        }, sink=pubs[-1])
        assert registered["observed"] is True
        job = session.agent_job
        await settle()
        job.feed(_tool_use("print1", "Read", file_path="prints.txt"))
        job.feed(_tool_result("print1"))
        await settle()
        active = [e for e in da._ACK_EDITORS.values() if not e._closed]
        assert active == [editor], f"mixed workers need ONE editor; editors={len(active)}, live IDs={sorted(wire.live())}"
        assert set(wire.live()) == {before_cli}, "the CLI must not post a second live card"
        assert {p.delegation_id for p in editor.card.parts} == {first.delegation_id, job.card_id}
        assert "3 subagents" in editor._last_text and "0 of 3 done" in editor._last_text
        for title in ("check hardware", "check software", "finding useful prints", "Sonnet 5.5"):
            assert title in editor._last_text
        assert pubs[-1].tracker.group_ids() == [], "the observer must not create a legacy roster"

        reply = await wire.adapter.send("123", "Here are the print ideas so far.")
        await asyncio.sleep(da.FOLLOW_DEBOUNCE_SECONDS + 0.15)
        current = next(iter(wire.live()))
        assert reply.message_id is not None
        assert current > int(reply.message_id) and before_cli not in wire.messages
        assert int(editor._message_id) == current
        assert editor in da._LIVE[da._inbox_key(wire.adapter, "123")]
        record = delegation_inflight.read_record(first.delegation_id)
        assert record is not None and record["card"]["message_id"] == str(current)
        event_start = len(wire.events)
        first.callbacks[0]("tool.completed", "read_file", "links.txt")
        first.callbacks[1]("tool.started", "terminal", "software check", {"command": "software-check"})
        job.feed(_tool_use("print2", "Bash", command="print-check"))
        await settle()
        edits = [(mid, kw) for kind, mid, kw in wire.events[event_start:] if kind == "edit"]
        assert edits and {mid for mid, _ in edits} == {current}, "both producers must edit the moved shared card"
        assert "software-check" in editor._last_text and "print-check" in editor._last_text
        assert all(kw.get("parse_mode") == "MarkdownV2" for _, kw in edits)
        assert all("**" not in kw["text"].replace("**>", ">") for _, kw in edits)

        # Both native children end; only their finished card stays beside their reply.
        for cb in first.callbacks:
            cb("subagent.complete", status="completed", summary="Plain summary: PC check ended.",
               task_outcome={"status": "done", "remaining": [], "owner": "parent",
                             "next_step": "Review the offline PC check."})
        assert native_part.checklist.state == ac.DONE
        assert all(kid.state == ac.DONE for kid in native_part.checklist.kids)
        da.start_completion_turn([first.delegation_id])
        native_result = await wire.adapter.send("123", "Here is the PC result.")
        da.start_completion_turn(None)
        await asyncio.sleep(da.FOLLOW_DEBOUNCE_SECONDS + 0.15)
        assert len(wire.live()) == 1
        assert "finding useful prints" in editor._last_text
        assert "check hardware" not in editor._last_text and "check software" not in editor._last_text
        # A completed two-child dispatch is titled "2 subagents", not its
        # former running job title. Match its real formatted result liner.
        native_liner = wire.adapter.format_message(editor.card.part_liner(native_part))
        finished_native = {mid for mid, kw in wire.messages.items() if kw["text"] == native_liner}
        assert len(finished_native) == 1 and finished_native.isdisjoint(opener_ids)
        assert native_result.message_id is not None
        assert min(finished_native) > int(native_result.message_id)
        first.callbacks[0]("tool.started", "terminal", "late native event", {"command": "late-native"})
        job.feed(_tool_result("print2"))
        reply = await wire.adapter.send("123", "another reply while the print search runs")
        await asyncio.sleep(da.FOLLOW_DEBOUNCE_SECONDS + 0.15)
        assert finished_native.issubset(wire.messages) and opener_ids.issubset(wire.messages)
        assert reply.message_id is not None
        assert next(iter(wire.live())) > int(reply.message_id)
        assert "late-native" not in editor._last_text and "check hardware" not in editor._last_text

        # Exit zero without a task report must be UNVERIFIED, never a fabricated success.
        await wire.adapter.send("123", "last reply arms follow before CLI exit")
        job.finish(0)
        assert ac._PARTS[job.card_id].checklist.state == ac.UNVERIFIED
        await settle()
        sends_before = sum(kind == "send" for kind, _, _ in wire.events)
        await asyncio.sleep(da.FOLLOW_DEBOUNCE_SECONDS + 0.15)
        assert editor._closed and not wire.live()
        assert sum(kind == "send" for kind, _, _ in wire.events) == sends_before
        da.start_completion_turn([job.card_id])
        await wire.adapter.send("123", "The print search ended; its result still needs review.")
        da.start_completion_turn(None)
        await settle()
        assert any("unverified" in kw["text"] and "finding useful prints" in kw["text"]
                   for kw in wire.messages.values())
        assert finished_native.issubset(wire.messages) and opener_ids.issubset(wire.messages)
        deletes = Counter(mid for kind, mid, _ in wire.events if kind == "delete")
        assert max(deletes.values()) == 1
        # Stable, non-private card-ID/state receipt in the raw test output.
        print({"say": bool(say), "original": original, "before_cli": before_cli,
               "joined_follow": current, "active_editors_at_join": len(active),
               "native_children": len(native_part.checklist.kids), "cli_final": ac.UNVERIFIED,
               "live_at_end": len(wire.live()), "shared_edits_after_follow": len(edits),
               "native_initial": native_initial, "native_final": native_part.checklist.state,
               "finished_native_ids": sorted(finished_native), "opener_ids": sorted(opener_ids),
               "max_deletes_per_id": max(deletes.values())})
    finally:
        da.start_completion_turn(None)
        if job is not None:
            job.finish(0)
        for pub in pubs:
            await pub.aclose()

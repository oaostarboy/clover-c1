"""A gateway restart starts the background subagents it cut off, once.

Real durable ledger / inflight records / session db in a scratch CLOVER_HOME,
the real resume pass and card publisher.  Only the platform adapter, the model
and the child work are fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from agent.delegation_activity import bind_activity_sink
from clover_state import SessionDB
from gateway import delegation_activity as da
from gateway import delegation_resume as dr
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.session_context import clear_session_vars, set_session_vars
from tools import async_delegation as ad
from tools import delegation_inflight as inflight
from tools.process_registry import process_registry

pytestmark = pytest.mark.unified_card

DID = "deleg_dead0001"
GOAL = "Count to 200 slowly, one terminal call with sleep 2 between each."


@pytest.fixture(autouse=True)
def _clean():
    ad._reset_for_tests(drain_timeout=5)
    inflight.reset_for_tests()
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    da._ACK_EDITORS.clear()
    _drain_queue()
    yield
    ad._reset_for_tests(drain_timeout=5)
    inflight.reset_for_tests()
    ac.reset_for_tests()
    da._ACK_EDITORS.clear()
    _drain_queue()


def _drain_queue():
    out = []
    while not process_registry.completion_queue.empty():
        out.append(process_registry.completion_queue.get_nowait())
    return out


class _Adapter:
    """Records sends / edits like a chat transport with edit-in-place."""

    platform = SimpleNamespace(value="telegram")

    def __init__(self, *, existing=("m-old",)):
        self.sends, self.edits, self.deleted = [], [], []
        self.existing = set(existing)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append(content)
        mid = f"m-new{len(self.sends)}"
        self.existing.add(mid)
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        if message_id not in self.existing:
            return SendResult(success=False, error="message to edit not found")
        self.edits.append({"id": message_id, "text": content})
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)
        return True


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="u1")


def _dispatch_record(delegation_id=DID, *, goals=(GOAL,), owner_pid=99999999, **over):
    """What delegate_task writes when a gateway chat dispatches in background
    (by default from a gateway process that is gone now)."""
    tokens = set_session_vars(
        platform="telegram", chat_id="100", chat_type="dm", session_key="agent:main:telegram:dm:100",
        user_id="u1",
    )
    try:
        from tools.async_delegation import _capture_routing_origin

        origin = _capture_routing_origin()
    finally:
        clear_session_vars(tokens)
    args = dict(
        tasks=[{"goal": g, "context": "ctx", "title": "Count slowly", "steps": ["count", "report"]} for g in goals],
        session_key="agent:main:telegram:dm:100", parent_session_id="sess-parent",
        origin=origin, top_context=None, top_role="leaf", model="m",
        handoff={"work": "counting slowly", "outcome": "reached 200", "say": "on it"},
        live_paths=["/tmp/live/task-0.log"], fanout_delivery="joined",
    )
    args.update(over)
    assert inflight.write_dispatch_record(delegation_id, **args)
    inflight.update_record(delegation_id, owner_pid=owner_pid, owner_started_at=1)
    return inflight.read_record(delegation_id)


def _ledger_row(delegation_id=DID, *, owner_pid=99999999, goals=(GOAL,)):
    """A dispatch row whose owning process (a previous gateway) is gone."""
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id, parent_session_id,
                state, dispatched_at, updated_at, delivery_state, delivery_attempts,
                owner_pid, owner_started_at, task_json, origin_session_id)
               VALUES (?, ?, '', ?, 'running', ?, ?, 'pending', 0, ?, 1, ?, '')""",
            (delegation_id, "agent:main:telegram:dm:100", "sess-parent", time.time(), time.time(),
             owner_pid, json.dumps({"goal": goals[0], "goals": list(goals), "is_batch": True,
                                    "fanout_delivery": "joined", "role": "leaf",
                                    "origin_platform": "telegram", "origin_chat_id": "100",
                                    "origin_chat_type": "dm"})),
        )


def _live_session(session_id="sess-parent"):
    db = SessionDB()
    db.create_session(session_id, source="telegram")
    db.close()


class _Runner:
    """What the resume pass asks of the gateway runner."""

    def __init__(self, adapter):
        self.adapter = adapter
        self._gateway_started_at = time.time() + 5  # records below are from a previous process

    async def _run_in_executor_with_context(self, fn, *args):
        return await asyncio.to_thread(fn, *args)


@pytest.fixture
def harness(monkeypatch):
    """Fake the model-backed parts: the parent agent and delegate_task itself,
    which opens the card and writes the new record exactly as the real one does."""
    state = SimpleNamespace(calls=[], adapter=_Adapter(), publisher=None, fail=None, status="dispatched")

    async def _build_parent(runner, record):
        pub = da.DelegationActivityPublisher(
            adapter=state.adapter, chat_id="100", loop=asyncio.get_running_loop(),
            min_interval=0.0, auto_heartbeat=False, expandable=True, combined=True,
        )
        state.publisher = pub
        return SimpleNamespace(platform="telegram"), _source(), pub

    def _delegate_task(*, tasks, role, background, require_background, parent_agent, **_):
        if state.fail:
            raise state.fail
        new_id = f"deleg_beef{len(state.calls) + 1:04d}"
        goals = [t["goal"] for t in tasks]
        state.calls.append({
            "tasks": tasks, "background": background, "require_background": require_background,
            "resume_card": inflight.current_resume_card(), "resume_work": inflight.current_resume_work(),
            "ctx": dict(inflight._RESUME_CONTEXT.get() or {}),
        })
        if state.status != "dispatched":
            return json.dumps({"status": "rejected", "error": "capacity"})
        fl.reserve_ack_part(new_id, None, goals)
        fl.note_own_dispatch(
            parent_agent, new_id, goals=goals, titles=[t.get("title") for t in tasks],
            task_steps=tasks[0].get("steps") if len(tasks) == 1 else (),
        )
        from tools.async_delegation import _capture_routing_origin

        inflight.write_dispatch_record(
            new_id, tasks=tasks, session_key="agent:main:telegram:dm:100",
            parent_session_id="sess-parent", origin=_capture_routing_origin(), top_role=role,
        )
        return json.dumps({"status": "dispatched", "delegation_id": new_id})

    monkeypatch.setattr(dr, "_build_parent", _build_parent)
    monkeypatch.setattr("tools.delegate_tool.delegate_task", _delegate_task)
    state.runner = _Runner(state.adapter)
    return state


# ── the record over the lifecycle ───────────────────────────────────────────


def test_record_is_written_private_with_the_full_spec():
    rec = _dispatch_record()
    path = inflight.record_path(DID)
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rec["delegation_id"] == DID and rec["parent_session_id"] == "sess-parent"
    assert rec["origin"]["origin_platform"] == "telegram" and rec["origin"]["origin_chat_id"] == "100"
    task = rec["tasks"][0]
    assert task["goal"] == GOAL and task["context"] == "ctx" and task["steps"] == ["count", "report"]
    assert rec["card_work"] == "counting slowly" and "say" not in (rec["handoff"] or {})
    assert rec["resumes"] == 0 and rec["live_paths"] == ["/tmp/live/task-0.log"]


def test_no_record_for_a_cli_dispatch_or_with_the_flag_off(monkeypatch):
    assert not inflight.write_dispatch_record(
        "d-cli", tasks=[{"goal": "g"}], session_key="k", parent_session_id="p", origin={},
    )
    monkeypatch.setattr(inflight, "resume_enabled", lambda: False)
    assert not inflight.write_dispatch_record(
        "d-off", tasks=[{"goal": "g"}], session_key="k", parent_session_id="p",
        origin={"origin_platform": "telegram", "origin_chat_id": "100"},
    )
    assert inflight.list_records() == []


def test_worktree_and_card_are_added_to_the_record():
    _dispatch_record()
    inflight.note_worktree(DID, 0, "/tmp/wt/sa-0")
    inflight.note_card(DID, platform="telegram", chat_id="100", thread_id="", message_id="m-old")
    rec = inflight.read_record(DID)
    assert rec["tasks"][0]["worktree"] == "/tmp/wt/sa-0"
    assert rec["card"] == {"platform": "telegram", "chat_id": "100", "thread_id": "", "message_id": "m-old"}


def test_record_removed_when_the_subagent_completes():
    done = threading.Event()
    tokens = set_session_vars(platform="telegram", chat_id="100", chat_type="dm",
                              session_key="agent:main:telegram:dm:100")
    try:
        handle = ad.dispatch_async_delegation_batch(
            goals=[GOAL], context=None, toolsets=None, role="leaf", model="m",
            session_key="agent:main:telegram:dm:100", parent_session_id="sess-parent",
            runner=lambda: (done.wait(10) and {"results": [{"task_index": 0, "status": "completed", "summary": "ok"}]}),
            delegation_id=DID, fanout_delivery="each",
        )
    finally:
        clear_session_vars(tokens)
    assert handle["status"] == "dispatched"
    _dispatch_record(DID)
    assert inflight.read_record(DID) is not None, "still running: the record stays"
    done.set()
    deadline = time.monotonic() + 5
    while inflight.read_record(DID) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert inflight.read_record(DID) is None, "completed: the record is gone"


def test_record_removed_when_the_subagent_fails():
    _dispatch_record()
    ad._finish_finalization(DID, "error")
    assert inflight.read_record(DID) is None


def test_a_child_cut_off_by_shutdown_keeps_its_record_and_reports_nothing():
    """interrupt_all at gateway shutdown ends the child 'interrupted': with the
    hold on, that is NOT a completion to report and the record must survive."""
    gate = threading.Event()
    tokens = set_session_vars(platform="telegram", chat_id="100", chat_type="dm",
                              session_key="agent:main:telegram:dm:100")
    try:
        ad.dispatch_async_delegation_batch(
            goals=[GOAL], context=None, toolsets=None, role="leaf", model="m",
            session_key="agent:main:telegram:dm:100", parent_session_id="sess-parent",
            runner=lambda: (gate.wait(10) and {"results": [{"task_index": 0, "status": "interrupted"}]}),
            interrupt_fn=gate.set, delegation_id=DID, fanout_delivery="each",
        )
    finally:
        clear_session_vars(tokens)
    _dispatch_record(DID)
    inflight.begin_shutdown_hold()
    assert ad.interrupt_all(reason="gateway shutdown") == 1
    time.sleep(0.6)
    assert inflight.read_record(DID) is not None
    assert [e for e in _drain_queue() if e.get("delegation_id") == DID] == []


def test_without_the_hold_an_interrupted_child_reports_as_before():
    gate = threading.Event()
    ad.dispatch_async_delegation_batch(
        goals=[GOAL], context=None, toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:100", parent_session_id="sess-parent",
        runner=lambda: (gate.wait(10) and {"results": [{"task_index": 0, "status": "interrupted"}]}),
        interrupt_fn=gate.set, delegation_id=DID, fanout_delivery="each",
    )
    ad.interrupt_all(reason="/stop")
    deadline = time.monotonic() + 5
    evts = []
    while time.monotonic() < deadline and not evts:
        evts = [e for e in _drain_queue() if e.get("delegation_id") == DID]
        time.sleep(0.05)
    assert evts, "a /stop is still reported"


# ── the ledger leaves a restartable job to the restart ──────────────────────


def test_abandoned_row_with_a_record_is_closed_silently_not_reported_unknown():
    _dispatch_record()
    _ledger_row()
    ad.recover_abandoned_delegations()
    assert ad.get_durable_delegation(DID)["state"] == "resumed"
    assert [e for e in _drain_queue() if e.get("delegation_id") == DID] == []


def test_abandoned_row_without_a_record_is_reported_as_before():
    _ledger_row()
    ad.recover_abandoned_delegations()
    assert ad.get_durable_delegation(DID)["state"] == "unknown"


def test_abandoned_row_is_reported_as_before_when_the_flag_is_off(monkeypatch):
    _dispatch_record()
    _ledger_row()
    monkeypatch.setattr(inflight, "resume_enabled", lambda: False)
    ad.recover_abandoned_delegations()
    assert ad.get_durable_delegation(DID)["state"] == "unknown"


# ── the restart pass ──────────────────────────────────────────────────────────


async def _pass(h):
    counts = await dr.resume_interrupted(h.runner)
    if h.publisher is not None:
        for _ in range(30):
            await asyncio.sleep(0)
        await h.publisher.drain()
        for _ in range(30):
            await asyncio.sleep(0)
    return counts


@pytest.mark.asyncio
async def test_restart_resumes_once_with_the_note_and_the_same_card(harness):
    _live_session()
    _dispatch_record()
    inflight.note_worktree(DID, 0, "/tmp/wt/sa-0")
    inflight.note_card(DID, platform="telegram", chat_id="100", thread_id="", message_id="m-old")
    _ledger_row()
    ad.recover_abandoned_delegations()

    counts = await _pass(harness)

    assert counts == {"resumed": 1, "reported": 0, "dropped": 0}
    assert len(harness.calls) == 1, "exactly one relaunch"
    call = harness.calls[0]
    assert call["background"] and call["require_background"]
    task = call["tasks"][0]
    assert task["goal"] == GOAL
    assert "interrupted by a restart" in task["context"]
    assert "/tmp/wt/sa-0" in task["context"] and "/tmp/live/task-0.log" in task["context"]
    assert "do not start over" in task["context"] and task["context"].endswith("ctx")
    assert task["title"] == "Count slowly" and task["steps"] == ["count", "report"]
    # the same card message was edited, no new message posted
    assert call["resume_card"]["message_id"] == "m-old"
    assert harness.adapter.edits and harness.adapter.edits[0]["id"] == "m-old"
    assert harness.adapter.sends == []
    assert "counting slowly" in harness.adapter.edits[0]["text"]
    # the old ledger row is closed silently, the new record counts the resume
    assert ad.get_durable_delegation(DID)["state"] == "resumed"
    assert inflight.read_record(DID) is None
    (new,) = inflight.list_records()
    assert new["resumes"] == 1 and new["delegation_id"] != DID
    # nothing was said to the parent
    assert [e for e in _drain_queue() if e.get("delegation_id") == DID] == []


@pytest.mark.asyncio
async def test_a_new_card_is_posted_when_the_old_message_is_gone(harness):
    _live_session()
    _dispatch_record()
    inflight.note_card(DID, platform="telegram", chat_id="100", thread_id="", message_id="m-deleted")
    _ledger_row()
    ad.recover_abandoned_delegations()

    counts = await _pass(harness)

    assert counts["resumed"] == 1
    assert harness.adapter.sends, "a fresh unified card"
    assert "counting slowly" in harness.adapter.sends[0]


@pytest.mark.asyncio
async def test_a_second_interruption_is_reported_not_resumed(harness):
    _live_session()
    _dispatch_record(DID)
    _ledger_row()
    ad.recover_abandoned_delegations()
    await _pass(harness)
    (second,) = inflight.list_records()
    assert second["resumes"] == 1
    # the restarted job is cut off by ANOTHER restart (its owner is gone too)
    inflight.update_record(second["delegation_id"], owner_pid=99999999, owner_started_at=1)
    _ledger_row(second["delegation_id"])
    ad.recover_abandoned_delegations()
    harness.calls.clear()
    harness.publisher = None

    counts = await _pass(harness)

    assert counts == {"resumed": 0, "reported": 1, "dropped": 0}
    assert harness.calls == [], "never resumed twice"
    evts = [e for e in _drain_queue() if e.get("delegation_id") == second["delegation_id"]]
    assert len(evts) == 1 and evts[0]["status"] == "unknown"
    assert "cut off a second time" in evts[0]["error"]
    assert "Tell the user plainly" in evts[0]["error"]
    assert inflight.list_records() == []


@pytest.mark.asyncio
async def test_a_record_older_than_six_hours_is_reported(harness):
    _live_session()
    _dispatch_record(dispatched_at=time.time() - 7 * 3600)
    _ledger_row()
    ad.recover_abandoned_delegations()

    counts = await _pass(harness)

    assert counts["reported"] == 1 and harness.calls == []
    (evt,) = [e for e in _drain_queue() if e.get("delegation_id") == DID]
    assert "6 hours ago" in evt["error"]


@pytest.mark.asyncio
async def test_a_job_whose_conversation_is_gone_is_reported(harness):
    _dispatch_record()  # no 'sess-parent' session in the db at all
    _ledger_row()
    ad.recover_abandoned_delegations()

    counts = await _pass(harness)

    assert counts["reported"] == 1 and harness.calls == []
    (evt,) = [e for e in _drain_queue() if e.get("delegation_id") == DID]
    assert "no longer exists" in evt["error"]


@pytest.mark.asyncio
async def test_a_conversation_that_was_reset_is_reported(harness):
    db = SessionDB()
    db.create_session("sess-parent", source="telegram")
    db.end_session("sess-parent", "new_session")
    db.close()
    _dispatch_record()
    _ledger_row()
    ad.recover_abandoned_delegations()

    counts = await _pass(harness)

    assert counts["reported"] == 1 and harness.calls == []


@pytest.mark.asyncio
async def test_a_failed_relaunch_is_reported_in_plain_words(harness):
    _live_session()
    _dispatch_record()
    _ledger_row()
    ad.recover_abandoned_delegations()
    harness.status = "rejected"

    counts = await _pass(harness)

    assert counts == {"resumed": 0, "reported": 1, "dropped": 0}
    (evt,) = [e for e in _drain_queue() if e.get("delegation_id") == DID]
    assert evt["status"] == "unknown" and "could not be started" in evt["error"]
    assert inflight.list_records() == []


@pytest.mark.asyncio
async def test_a_crash_while_relaunching_is_reported_not_retried(harness):
    _live_session()
    _dispatch_record()
    _ledger_row()
    ad.recover_abandoned_delegations()
    harness.fail = RuntimeError("boom")

    counts = await _pass(harness)

    assert counts["reported"] == 1 and len(harness.calls) == 0
    assert [e for e in _drain_queue() if e.get("delegation_id") == DID]


@pytest.mark.asyncio
async def test_flag_off_means_no_resume_and_the_old_report(harness, monkeypatch):
    _live_session()
    _dispatch_record()
    _ledger_row()
    monkeypatch.setattr(inflight, "resume_enabled", lambda: False)
    ad.recover_abandoned_delegations()

    counts = await _pass(harness)

    assert harness.calls == []
    assert counts["resumed"] == 0
    assert ad.get_durable_delegation(DID)["state"] == "unknown"
    assert inflight.list_records() == [], "stale records are cleaned up"


@pytest.mark.asyncio
async def test_a_job_dispatched_by_this_process_is_left_alone(harness):
    _live_session()
    _dispatch_record(dispatched_at=time.time() + 60)  # newer than this process's start
    counts = await _pass(harness)
    assert counts == {"resumed": 0, "reported": 0, "dropped": 0} and harness.calls == []
    assert inflight.read_record(DID) is not None


@pytest.mark.asyncio
async def test_a_job_whose_owner_still_runs_is_left_alone(harness):
    _live_session()
    rec = _dispatch_record(owner_pid=os.getpid())
    inflight.update_record(DID, owner_started_at=None)
    assert rec["owner_pid"] == os.getpid()
    harness.runner._gateway_started_at = time.time() + 60
    counts = await _pass(harness)
    assert harness.calls == [] and counts["resumed"] == 0
    assert inflight.read_record(DID) is not None


@pytest.mark.asyncio
async def test_a_finished_job_is_just_cleaned_up(harness):
    _live_session()
    _dispatch_record()
    _ledger_row()
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='completed' WHERE delegation_id=?", (DID,))

    counts = await _pass(harness)

    assert counts["dropped"] == 1 and harness.calls == []
    assert inflight.list_records() == []


@pytest.mark.asyncio
async def test_a_fanout_resumes_only_the_helpers_that_had_not_finished(harness):
    _live_session()
    goals = ("audit alpha", "audit beta", "audit gamma")
    _dispatch_record(goals=goals)
    _ledger_row(goals=goals)
    # helper 0 had already reported before the restart
    ad.publish_batch_child_completion(
        {"task_index": 0, "status": "completed", "summary": "alpha findings"}, delegation_id=DID,
    )
    ad.recover_abandoned_delegations()

    counts = await _pass(harness)

    assert counts["resumed"] == 1
    assert [t["goal"] for t in harness.calls[0]["tasks"]] == ["audit beta", "audit gamma"]


def test_resumed_task_notes_say_where_the_earlier_work_is():
    rec = {
        "tasks": [{"goal": "g", "context": None, "worktree": "/w/x"}, {"goal": "h"}],
        "live_paths": ["/t/0.log", "/t/1.log"],
    }
    a, b = dr.build_resume_tasks(rec, [0, 1])
    assert "/w/x" in a["context"] and "/t/0.log" in a["context"]
    assert "/t/1.log" in b["context"] and "/w/" not in b["context"]
    assert a["context"].startswith("You were interrupted by a restart.")


def test_report_lands_in_the_queue_once():
    _dispatch_record()
    _ledger_row()
    ad.recover_abandoned_delegations()
    assert ad.report_unresumed(DID, "because.")
    assert not ad.report_unresumed(DID, "because."), "a second report is a no-op"
    assert len([e for e in _drain_queue() if e.get("delegation_id") == DID]) == 1


def test_source_is_rebuilt_from_the_stored_route_for_a_raw_session_key():
    """The API server's session key is the caller's raw id: no layout to parse."""
    src = dr._source_from_origin(
        {"origin_platform": "api_server", "origin_chat_id": "vegas-resume-1", "user_id": "u9"},
        {},
    )
    assert src is not None and src.platform.value == "api_server"
    assert src.chat_id == "vegas-resume-1" and src.user_id == "u9"
    assert dr._source_from_origin({"origin_platform": "nonsense", "origin_chat_id": "1"}, {}) is None
    assert dr._source_from_origin({"origin_platform": "telegram"}, {}) is None

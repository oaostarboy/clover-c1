"""Quieter notices for background helpers (rel-p1b).

Real ``SessionStore`` / ``SessionDB`` in a temp dir and a real async-delegation
batch; only the notice transport is fake.

* A helper the user cancelled (its owning conversation ended at a user
  boundary and the helper was interrupted) is recorded, listable with
  ``/results``, and owes NO notice.
* A helper that finished with a result and then could not be delivered is
  still noticed.
* Notices pending together for one chat go out as ONE message.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import AsyncMock

import pytest

import tools.async_delegation as ad
from gateway.inbox_notices import NoticeSweepState, sweep_inbox_notices
from gateway.platforms.base import MessageEvent
from tests.gateway.test_route_ownership_delegated_child import _Env


@pytest.fixture
def env(tmp_path):
    e = _Env(tmp_path)
    runner = e.runner
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = {}
    runner._completion_delivery_retention = 64
    runner._inject_watch_notification = AsyncMock(return_value=True)
    ad._reset_for_tests()
    try:
        yield e
    finally:
        ad._reset_for_tests()
        from tools.process_registry import process_registry

        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()
        e.db.close()


def _interrupted(index):
    return {
        "task_index": index, "status": "interrupted", "summary": None,
        "error": "Operation interrupted", "api_calls": 1,
        "duration_seconds": 8.0, "exit_reason": "interrupted",
    }


def _finished(index):
    return {
        "task_index": index, "status": "completed", "summary": f"result {index}",
        "error": None, "api_calls": 4, "duration_seconds": 20.0,
        "exit_reason": "completed",
    }


def _run_batch_across_new(env, goals, results, *, batch_id="deleg_bd606446", reset=True):
    """Dispatch a batch from the current conversation, send ``/new`` while its
    children run, then let them end with ``results``. Returns the child events."""
    from tools.process_registry import process_registry

    gate = threading.Event()

    def runner():
        gate.wait(10)
        for result in results:
            ad.publish_batch_child_completion(result, delegation_id=batch_id)
        return {"results": results}

    handle = ad.dispatch_async_delegation_batch(
        goals=goals, context=None, toolsets=None, role="leaf", model="m",
        session_key=env.key, parent_session_id=env.parent, runner=runner,
        delegation_id=batch_id,
    )
    assert handle["status"] == "dispatched"
    if reset:
        env.store.reset_session(env.key)  # the human sends /new
    gate.set()
    events = []
    deadline = time.monotonic() + 10
    while len(events) < len(results) and time.monotonic() < deadline:
        if process_registry.completion_queue.empty():
            time.sleep(0.02)
            continue
        evt = process_registry.completion_queue.get_nowait()
        if evt.get("batch_delegation_id") == batch_id:
            events.append(evt)
    assert len(events) == len(results), "children never completed"
    return events


async def _deliver_all(env, events):
    for evt in events:
        assert await env.runner._deliver_completion_notification("done", evt) is None
    env.runner._inject_watch_notification.assert_not_awaited()


class _Sender:
    def __init__(self):
        self.calls = []

    async def __call__(self, record, text):
        self.calls.append((record["key"], text))


@pytest.mark.asyncio
async def test_new_after_dispatch_leaves_cancelled_helpers_unannounced_but_listed(env):
    old_session = env.parent
    events = _run_batch_across_new(
        env, ["Task A", "Task B", "Task C"], [_interrupted(i) for i in range(3)],
    )
    await _deliver_all(env, events)

    records = [env.db.inbox_get(f"deleg:{e['delegation_id']}") for e in events]
    assert all(r is not None and r["state"] == "dropped" for r in records)
    assert all(r["notice_state"] is None for r in records), "a cancelled helper owes no notice"
    send = _Sender()
    assert (await sweep_inbox_notices(env.db, send))["sent"] == 0
    assert send.calls == []

    # The old conversation still shows them once the user resumes it.
    env.store.switch_session(env.key, old_session)
    listing = await env.runner._handle_results_command(
        MessageEvent(text="/results", source=env.source, message_id="m1")
    )
    for evt in events:
        assert f"deleg:{evt['delegation_id']}" in listing


@pytest.mark.asyncio
async def test_helper_that_finished_then_lost_its_route_is_still_noticed(env):
    events = _run_batch_across_new(env, ["Task A"], [_finished(0)])
    await _deliver_all(env, events)

    send = _Sender()
    assert (await sweep_inbox_notices(env.db, send))["sent"] == 1
    assert len(send.calls) == 1 and "Task A" in send.calls[0][1]


@pytest.mark.asyncio
async def test_two_finished_helpers_in_one_batch_get_one_message(env):
    events = _run_batch_across_new(
        env, ["Task A", "Task B"], [_finished(0), _finished(1)],
    )
    await _deliver_all(env, events)

    send = _Sender()
    out = await sweep_inbox_notices(env.db, send)

    assert out["sent"] == 2 and len(send.calls) == 1
    text = send.calls[0][1]
    assert text.startswith("Background results not delivered to the assistant: ")
    assert "Task A" in text and "Task B" in text
    assert "/results" in text and "/resume" in text


@pytest.mark.asyncio
async def test_only_the_finished_helpers_of_a_mixed_batch_are_named(env):
    events = _run_batch_across_new(
        env, ["Task A", "Task B", "Task C"],
        [_finished(0), _interrupted(1), _interrupted(2)],
    )
    await _deliver_all(env, events)

    send = _Sender()
    await sweep_inbox_notices(env.db, send)

    assert len(send.calls) == 1
    assert "Task A" in send.calls[0][1]
    assert "Task B" not in send.calls[0][1] and "Task C" not in send.calls[0][1]


@pytest.mark.asyncio
async def test_interrupted_helper_without_a_user_boundary_is_still_noticed(env):
    """The exemption is for helpers the USER ended: an owner that ended for an
    unknown/automatic reason keeps its notice."""
    env.db.end_session(env.parent, "suspended")
    events = _run_batch_across_new(env, ["Task A"], [_interrupted(0)], reset=False)
    await _deliver_all(env, events)

    record = env.db.inbox_get(f"deleg:{events[0]['delegation_id']}")
    assert record["state"] == "dropped" and record["notice_state"] == "pending"


def _pending(db, key, *, title, chat_id="100", thread_id=None):
    db.inbox_put({
        "key": key, "profile": "default", "platform": "telegram", "chat_id": chat_id,
        "thread_id": thread_id, "session_key": f"agent:main:telegram:dm:{chat_id}",
        "owner_root_id": "root", "kind": "delegation", "wake": 0, "title": title,
        "payload_json": "{}", "shown_to_user": 0,
    })
    assert db.inbox_drop(key, "unowned:test")


@pytest.mark.asyncio
async def test_five_batches_pending_on_one_chat_are_one_message(env):
    for n in range(5):
        _pending(env.db, f"deleg:b{n}", title=f"Batch {n} " + "x" * 80)
    _pending(env.db, "deleg:other", title="Other chat", chat_id="200")
    send = _Sender()

    out = await sweep_inbox_notices(env.db, send)

    by_chat = {key: text for key, text in send.calls}
    assert out["sent"] == 6 and len(send.calls) == 2
    text = next(t for k, t in send.calls if k != "deleg:other")
    assert text.startswith("Background results not delivered to the assistant: ")
    assert "Batch 0" in text and "Batch 1" in text and "Batch 2" in text
    assert "Batch 3" not in text and "Batch 4" not in text
    assert "(+2 more)" in text
    assert "x" * 61 not in text, "each listed title is cut to 60 characters"
    assert text.endswith(
        "Use /results in that conversation (resume it with /resume if you started a new one)."
    )
    assert "Other chat" in by_chat["deleg:other"]
    assert all(
        env.db.inbox_get(f"deleg:b{n}")["notice_state"] == "sent" for n in range(5)
    )


@pytest.mark.asyncio
async def test_ambiguous_group_send_marks_every_included_notice_uncertain(env):
    for n in range(3):
        _pending(env.db, f"deleg:b{n}", title=f"Batch {n}")
    attempts = []

    async def boom(record, text):
        attempts.append(text)
        raise TimeoutError("socket died mid-send")

    state = NoticeSweepState()
    out = await sweep_inbox_notices(env.db, boom, state=state)
    again = await sweep_inbox_notices(env.db, boom, state=state)

    assert out["uncertain"] == 3 and again == {"sent": 0, "uncertain": 0, "deferred": 0}
    assert len(attempts) == 1, "no resend"
    assert all(
        env.db.inbox_get(f"deleg:b{n}")["notice_state"] == "uncertain" for n in range(3)
    )


@pytest.mark.asyncio
async def test_unsent_group_goes_back_to_pending_together_and_is_retried(env):
    from gateway.inbox_notices import NoticeNotSent

    for n in range(3):
        _pending(env.db, f"deleg:b{n}", title=f"Batch {n}")
    offline = {"on": True}
    sent = []

    async def send(record, text):
        if offline["on"]:
            raise NoticeNotSent("no live adapter")
        sent.append(text)

    state = NoticeSweepState()
    clock = {"t": 0.0}
    out = await sweep_inbox_notices(env.db, send, state=state, clock=lambda: clock["t"])
    assert out["deferred"] == 3
    assert all(env.db.inbox_get(f"deleg:b{n}")["notice_state"] == "pending" for n in range(3))

    offline["on"] = False
    clock["t"] = 10_000.0
    assert (await sweep_inbox_notices(env.db, send, state=state, clock=lambda: clock["t"]))["sent"] == 3
    assert len(sent) == 1

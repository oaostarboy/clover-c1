"""Request-bound foreground allowance and owned handoff (Patch 2).

One human request owns one non-renewable foreground allowance. Declaring
again, renaming the todo, a new phase label, or a late declaration completion
must not extend it, and an accepted background dispatch closes the root's
heavy work for that request. Only a new human request (``begin_turn`` with
``preserve=False``) starts a new allowance.

Real ``DelegationCheckpoint`` and real ``admit``; the router tests go through
``AIAgent._execute_tool_calls`` with the real ``todo`` and ``delegate_task``
tools. Only a child's model conversation is faked.
"""
from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest

import tools.async_delegation as ad
import tools.delegate_tool as dt
from agent import delegation_checkpoint as dc
from tests.agent.test_delegation_checkpoint import (  # noqa: F401  (child_runs is a fixture)
    _agent,
    _delegate,
    _direct,
    _spawn,
    _write,
    child_runs,
    run_batch,
    sync_spawn,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _root(clock=None):
    """A conversational root with a real checkpoint (no model, no tools)."""
    root = SimpleNamespace(
        valid_tool_names={"todo", "delegate_task"},
        platform="cli",
        _delegate_depth=0,
        _subagent_id=None,
        session_id="root-session",
        _interrupt_requested=False,
        _active_children=[],
        _active_children_lock=None,
        _delegation_checkpoint=dc.DelegationCheckpoint(clock=clock or _Clock()),
    )
    return root


def _cp(root):
    return root._delegation_checkpoint


def _work(root, call_id="w"):
    verdict = dc.admit(root, "write_file", {}, call_id)
    if verdict.admission is not None:
        _cp(root).finish(verdict.admission)
    return verdict


def _spend(root, n):
    for i in range(n):
        assert not _work(root, f"spend-{i}").blocked


def _spawn_verdict(root, **args):
    args.setdefault("goal", "Build and verify the whole reusable explainer.")
    return dc.admit(root, "delegate_task", args, "spawn-call")


def _accept(root, delegation_id="deleg-1", goals=("Build and verify.",)):
    ticket = dc.ticket_for(root)
    return ticket.accept_handoff(
        delegation_id=delegation_id, goals=list(goals), subagent_ids=["sa-1"]
    )


# ── the allowance cannot be renewed by declaring again ─────────────────────

def test_redeclare_direct_after_exhaustion_stays_blocked():
    root = _root()
    assert _cp(root).declare("direct", "phase one")
    _spend(root, 5)
    assert _work(root).block_code == dc.FOREGROUND_EXHAUSTED

    assert _cp(root).declare("direct", "phase two: same job, new label")
    verdict = _work(root)

    assert verdict.blocked and verdict.block_code == dc.FOREGROUND_EXHAUSTED
    assert _cp(root).snapshot()["used"] == 5


def test_redeclare_before_exhaustion_does_not_reset_the_ledger():
    root = _root()
    _cp(root).declare("direct", "first")
    _spend(root, 3)
    _cp(root).declare("direct", "restated")
    _spend(root, 2)

    assert _work(root).blocked, "restating the choice bought five more calls"


def test_time_budget_is_not_extended_by_declarations():
    clock = _Clock()
    root = _root(clock)
    _cp(root).declare("direct", "go")
    assert not _work(root).blocked
    clock.now += 119.0
    _cp(root).declare("direct", "still going")
    assert not _work(root).blocked
    clock.now += 1000.0
    _cp(root).declare("direct", "still going, honest")

    assert _work(root).block_code == dc.FOREGROUND_EXHAUSTED


def test_declare_never_touches_the_request_ledger():
    root = _root()
    cp = _cp(root)
    cp.declare("direct", "a")
    _spend(root, 2)
    before = cp.snapshot()

    cp.declare("delegate", "b")
    cp.declare("direct", "c")
    after = cp.snapshot()

    for key in ("used", "phase", "request_id"):
        assert after[key] == before[key]
    assert after["generation"] > before["generation"], "stale tickets must still die"


def test_todo_rename_and_new_phase_label_do_not_reset(tmp_path):
    agent = _agent()
    run_batch(agent, [_direct("Phase 1: gather inputs.")])
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f"p1-{i}.txt")])
    run_batch(agent, [("todo", {"todos": [
        {"id": "p2", "content": "Phase 2: render the video", "status": "in_progress"}]})])
    run_batch(agent, [_direct("Phase 2: render the video.")])
    (blocked,) = run_batch(agent, [_write(tmp_path / "p2.txt")])

    assert not (tmp_path / "p2.txt").exists(), "a relabelled phase ran heavy work in the root"
    assert blocked["error_type"] == dc.FOREGROUND_EXHAUSTED


def test_compression_does_not_touch_the_ledger(tmp_path, monkeypatch):
    from agent.delegation_checkpoint import get_checkpoint

    agent = _agent()
    run_batch(agent, [_direct()])
    for i in range(3):
        run_batch(agent, [_write(tmp_path / f"c{i}.txt")])
    before = get_checkpoint(agent).snapshot()
    before_request = get_checkpoint(agent).request_id

    messages = [{"role": "user", "content": f"m{i}"} for i in range(4)]
    compressed = []

    def compress(self, msgs, *a, **k):
        compressed.append(len(msgs))
        return list(msgs)

    monkeypatch.setattr(type(agent.context_compressor), "compress", compress)
    agent._compress_context(messages, "system", approx_tokens=10)

    assert compressed, "the real compression entry never reached the compressor"
    after = get_checkpoint(agent).snapshot()
    assert get_checkpoint(agent).request_id == before_request
    for key in ("used", "phase", "state"):
        assert after[key] == before[key]


# ── late / stale completions grant nothing ─────────────────────────────────

def test_late_declaration_completion_cannot_unexhaust_the_request():
    root = _root()
    cp = _cp(root)
    cp.declare("direct", "first")
    owner = dc.DeclarationOwner(cp, cp.current_generation())
    _spend(root, 5)
    assert _work(root).blocked

    dc.record_declaration(root, {"mode": "direct", "reason": "late"}, owner)

    assert _work(root).block_code == dc.FOREGROUND_EXHAUSTED


def test_declaration_from_a_previous_request_is_ignored():
    root = _root()
    cp = _cp(root)
    cp.declare("direct", "first")
    owner = dc.DeclarationOwner(cp, cp.current_generation())
    cp.begin_turn(preserve=False, settings=cp.settings)

    assert dc.record_declaration(root, {"mode": "direct", "reason": "late"}, owner) is False
    assert cp.snapshot()["state"] == dc.UNDECIDED


def test_new_human_request_starts_a_fresh_allowance_and_keeps_ownership():
    root = _root()
    cp = _cp(root)
    cp.declare("direct", "first")
    first_request = cp.request_id
    assert _accept(root)
    assert cp.snapshot()["phase"] == dc.PHASE_HANDED_OFF

    cp.begin_turn(preserve=False, settings=cp.settings)

    snap = cp.snapshot()
    assert cp.request_id != first_request
    assert (snap["phase"], snap["used"], snap["state"]) == (
        dc.PHASE_FOREGROUND, 0, dc.UNDECIDED)
    assert "deleg-1" in cp._owned, "a late receipt must still be attributable to its request"
    assert cp._owned["deleg-1"].request_id == first_request


def test_trusted_delivery_keeps_the_exhausted_ledger():
    root = _root()
    cp = _cp(root)
    cp.declare("direct", "go")
    _spend(root, 5)
    assert _work(root).blocked
    request = cp.request_id

    dc.begin_turn(root, dc.PRESERVE_DISPLAY_KIND)

    assert cp.request_id == request
    assert _work(root).block_code == dc.FOREGROUND_EXHAUSTED


# ── an accepted worker start owns the remaining phase ──────────────────────

def test_accepted_handoff_closes_heavy_root_work_and_spawning():
    root = _root()
    cp = _cp(root)
    cp.declare("delegate", "Long phase.")
    assert _accept(root)

    work = _work(root)
    spawn = _spawn_verdict(root)

    assert work.block_code == dc.HANDOFF_ACTIVE
    assert spawn.block_code == dc.SPAWN_CLOSED
    assert cp.exit_armed is True


def test_dummy_background_child_closes_the_root_instead_of_licensing_it(tmp_path, child_runs):
    agent = _agent()
    target = tmp_path / "heavy.txt"

    _, dispatched = run_batch(agent, [_delegate(), _spawn("noop")])
    (blocked,) = run_batch(agent, [_write(target)])

    assert dispatched["status"] == "dispatched"
    assert not target.exists(), "a trivial helper licensed heavy root work"
    assert blocked["error_type"] == dc.HANDOFF_ACTIVE


def test_handoff_bundle_blocks_later_calls_with_one_receipt_each(tmp_path, child_runs):
    agent = _agent()
    target = tmp_path / "after.txt"

    results = run_batch(
        agent,
        [_delegate(), _spawn("lane one"), _write(target, "x"), _spawn("lane two")],
    )

    _, dispatched, write_block, second_block = results
    assert dispatched["status"] == "dispatched"
    assert write_block["error_type"] == dc.HANDOFF_ACTIVE
    assert second_block["error_type"] == dc.SPAWN_CLOSED
    assert not target.exists()
    assert child_runs == ["lane one"], "the second dispatch must never construct a child"


def test_concurrent_read_only_calls_after_a_dispatch_see_the_handoff(tmp_path, child_runs):
    agent = _agent()
    files = []
    for i in range(3):
        f = tmp_path / f"r{i}.txt"
        f.write_text(f"content-{i}")
        files.append(f)

    results = run_batch(
        agent,
        [_direct("Read first."), ("read_file", {"path": str(files[0])}),
         _spawn("lane one"),
         ("read_file", {"path": str(files[1])}), ("read_file", {"path": str(files[2])})],
    )

    _, before, dispatched, after_a, after_b = results
    assert "content-0" in json.dumps(before), "work admitted before the handoff must run"
    assert dispatched["status"] == "dispatched"
    for blocked in (after_a, after_b):
        assert blocked["error_type"] == dc.HANDOFF_ACTIVE
        assert "content-" not in json.dumps(blocked)


def test_stale_ticket_cannot_accept_a_handoff_and_leaves_the_job_running():
    root = _root()
    cp = _cp(root)
    cp.declare("delegate", "Long phase.")
    ticket = dc.ticket_for(root)
    interrupted = []
    release = threading.Event()
    result = ad.dispatch_async_delegation_batch(
        goals=["held"], context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: (release.wait(60), {"results": []})[1],
        interrupt_fn=lambda: interrupted.append(1),
        max_async_children=1,
    )
    try:
        assert result["status"] == "dispatched"
        cp.begin_turn(preserve=False, settings=cp.settings)

        accepted = ticket.accept_handoff(
            delegation_id=result["delegation_id"], goals=["held"], subagent_ids=[])

        assert accepted is False
        assert ad.active_count() == 1, "refusing the handoff must not cancel the job"
        assert interrupted == []
        assert (cp.phase, cp.exit_armed) == (dc.PHASE_FOREGROUND, False)
    finally:
        release.set()


def test_handoff_binds_request_reason_and_the_durable_receipt_ids():
    root = _root()
    cp = _cp(root)
    cp.declare("delegate", "Render the long video in a worker.")
    first_request = cp.request_id
    assert _accept(root, "single", goals=("one goal",))

    # A fan-out is accepted by a later request; the earlier handoff is kept.
    cp.begin_turn(preserve=False, settings=cp.settings)
    cp.declare("delegate", "Two independent lanes.")
    second_request = cp.request_id
    assert dc.ticket_for(root).accept_handoff(
        delegation_id="batch", goals=["g" * 400, "second"], subagent_ids=["a", "b"])

    single, batch = cp._owned["single"], cp._owned["batch"]
    assert (single.request_id, batch.request_id) == (first_request, second_request)
    assert first_request != second_request
    assert single.declared_reason == "Render the long video in a worker."
    assert batch.declared_reason == "Two independent lanes."
    assert single.receipt_ids == ("single",)
    assert batch.receipt_ids == ("batch:child:0", "batch:child:1")
    assert all(len(g) <= 160 for g in batch.goals)
    assert batch.subagent_ids == ("a", "b")


def test_exhausted_root_may_only_leave_through_a_background_spawn():
    root = _root()
    _cp(root).declare("direct", "go")
    _spend(root, 5)
    assert _work(root).blocked

    assert not _spawn_verdict(root).blocked
    assert _accept(root)
    assert _cp(root).snapshot()["phase"] == dc.PHASE_HANDED_OFF


def test_control_actions_stay_available_in_every_phase():
    root = _root()
    _cp(root).declare("direct", "go")
    _spend(root, 5)
    assert _work(root).blocked
    assert _accept(root)

    for action in ("list", "steer", "stop"):
        assert not dc.admit(root, "delegate_task", {"action": action}, "c").blocked
    assert not dc.admit(root, "todo", {}, "t").blocked


def test_a_rejected_dispatch_leaves_the_foreground_phase(tmp_path, child_runs):
    agent = _agent()
    target = tmp_path / "out.txt"

    run_batch(agent, [_direct("Try a helper first."), ("delegate_task", {})])
    (written,) = run_batch(agent, [_write(target, "ok")])

    assert target.read_text() == "ok", "a failed dispatch closed the foreground phase"
    assert not (isinstance(written, dict) and written.get("error_type"))


def test_the_spawn_gate_ignores_the_deprecated_background_argument():
    verdicts = {}
    for label, args in (("true", {"background": True}), ("false", {"background": False}), ("none", {})):
        root = _root()
        _cp(root).declare("direct", "go")
        _spend(root, 5)
        assert _work(root).blocked
        verdicts[label] = _spawn_verdict(root, **args).blocked
    assert verdicts == {"true": False, "false": False, "none": False}

    for args in ({"background": True}, {"background": False}, {}):
        root = _root()
        _cp(root).declare("delegate", "go")
        assert _accept(root)
        assert _spawn_verdict(root, **args).block_code == dc.SPAWN_CLOSED


def test_block_text_never_steers_with_the_ignored_argument_or_a_renewal(tmp_path):
    root = _root()
    _cp(root).declare("direct", "go")
    _spend(root, 5)
    exhausted = _work(root)
    other = _root()
    _cp(other).declare("delegate", "go")
    assert _accept(other)
    handoff_work, handoff_spawn = _work(other), _spawn_verdict(other)

    for verdict in (exhausted, handoff_work, handoff_spawn):
        assert verdict.blocked
        text = verdict.block_message
        assert not re.search(r"background\s*=", text), text
        assert "renew" not in text.lower(), text
        recovery = json.loads(verdict.block_result)["recovery"]
        assert "todo" not in json.dumps(recovery), "a recovery path must not offer re-declaring"


# ── synchronous children spend the allowance ───────────────────────────────

def test_a_synchronous_child_spends_a_work_unit(tmp_path, child_runs):
    agent = _agent()
    run_batch(agent, [_direct("Inline lanes.")])
    for i in range(5):
        sync_spawn(agent, goal=f"lane {i}")
    (blocked,) = run_batch(agent, [_write(tmp_path / "after.txt")])

    assert child_runs == [f"lane {i}" for i in range(5)]
    assert blocked["error_type"] == dc.FOREGROUND_EXHAUSTED


def test_a_rejected_synchronous_construction_spends_nothing(tmp_path, child_runs, monkeypatch):
    from agent.delegation_checkpoint import get_checkpoint

    agent = _agent()
    run_batch(agent, [_delegate()])

    def refuse(**kw):
        raise ValueError("pinned delegation.command is not on PATH")

    monkeypatch.setattr(dt, "_build_child_agent", refuse)
    sync_spawn(agent, goal="never built")

    assert get_checkpoint(agent).snapshot()["used"] == 0
    assert child_runs == []


# ── controls ───────────────────────────────────────────────────────────────

def test_short_task_stays_direct_within_the_allowance(tmp_path):
    agent = _agent()
    run_batch(agent, [_direct("One-file fix.")])
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f"f{i}.txt", "ok")])

    assert all((tmp_path / f"f{i}.txt").read_text() == "ok" for i in range(5))
    from agent.delegation_checkpoint import get_checkpoint

    assert get_checkpoint(agent).snapshot()["phase"] == dc.PHASE_FOREGROUND


def test_noneligible_callers_are_never_gated():
    root = _root()
    root._delegation_checkpoint_exempt = "batch_runner"
    _cp(root).declare("direct", "go")
    _cp(root).phase = dc.PHASE_HANDED_OFF

    assert not _work(root).blocked
    assert not _spawn_verdict(root).blocked


# ── the synchronous spawn gate (unreachable via the model path today) ──────
# The model path backgrounds every eligible root (depth 0), so these steer the
# dispatcher's own depth rule; they pin the gate against that rule changing.

@pytest.fixture
def sync_gate(monkeypatch):
    monkeypatch.setattr(dt, "_model_background_value", lambda args, agent=None: False)


def test_sync_spawn_right_after_the_last_work_call_is_blocked(sync_gate):
    root = _root()
    _cp(root).declare("direct", "go")
    _spend(root, 5)

    verdict = _spawn_verdict(root)

    assert verdict.block_code == dc.FOREGROUND_EXHAUSTED
    assert _cp(root).snapshot()["phase"] == dc.PHASE_EXHAUSTED


def test_sync_spawn_after_clock_expiry_is_blocked(sync_gate):
    clock = _Clock()
    root = _root(clock)
    _cp(root).declare("direct", "go")
    assert not _work(root).blocked
    clock.now += 500

    assert _spawn_verdict(root).block_code == dc.FOREGROUND_EXHAUSTED


def test_sync_spawn_inside_the_allowance_is_admitted_and_not_charged_by_the_gate(sync_gate):
    root = _root()
    _cp(root).declare("direct", "go")

    assert not _spawn_verdict(root).blocked
    assert _cp(root).snapshot()["used"] == 0, "charging belongs to the real child start"


def test_exhausted_phase_refuses_a_synchronous_spawn(sync_gate):
    root = _root()
    _cp(root).declare("direct", "go")
    _spend(root, 5)
    assert _work(root).blocked

    assert _spawn_verdict(root).block_code == dc.FOREGROUND_EXHAUSTED

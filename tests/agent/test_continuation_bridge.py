"""Blocker-clear continuation bridge: monitor event -> owned next-stage admission.

Real lifecycle, as in test_router_staged_continuation: real conversation loop,
executor funnel, SessionDB, async delegation registry and durable rows, real
notepad/workstream ledger, real cron scheduler consumer.  Only the provider,
the external monitor *source* and a helper's model conversation are scripted.

The original runtime binds a staged plan to the stage-before's durable row; a
human-approved, monitor-linked open blocker in the same canonical scope holds
the next stage.  A real monitor change is evidence only: it reaches the next
stage solely through the runtime's own claim + dispatch admission.
"""
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

import importlib

from agent import delegation_router as dr, router_backstop as rb
from cron import jobs, monitor, notepad as np, scheduler


class _Live:
    """Resolve a module at use time, so a test elsewhere in a long process
    that reloads it cannot leave this module holding a stale copy whose
    classes/secret differ from the ones SessionDB imports lazily."""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        return getattr(importlib.import_module(self._name), attr)


origin = _Live("agent.workstream_origin")
ws = _Live("agent.workstreams")
from tests.agent.test_delegation_router import aux  # noqa: F401
from tests.agent.test_router_staged_continuation import (  # noqa: F401
    STAGE_ONE, STAGE_TWO, _all_ids, _completion_turn, _dispatch_stage_one,
    _finish_and_deliver, _new_dispatch_ids, _staged, _wait, helpers, isolated,
)
from tests.agent.test_router_sol_review import root_agent
import tools.async_delegation as ad
from clover_state import SessionDB

pytestmark = pytest.mark.real_delegation_router

BLOCKER = "CI is not green yet; the read-only checklist must wait"


class _Event:
    internal = False


def _receipt(db, session_id="human-1"):
    """A real receipt for a real persisted user row (real mint/redeem/write)."""
    event = _Event()
    assert origin.mint_human_ingress(event, platform="telegram")
    ingress = origin.redeem_human_ingress(
        event, platform="telegram", profile=ws.current_profile(), session_id=session_id,
        session_key="k", run_generation=1, is_current=lambda: True)
    row = {"role": "user", "content": "x", "human_ingress": ingress}
    db.append_messages_batch(session_id, [row])
    got = db.get_human_origin(session_id, row["_row_id"])
    assert got is not None
    return got


@pytest.fixture
def world(monkeypatch, tmp_path, aux, helpers):
    """Approved linked blocker + root agent in the same canonical scope."""
    import yaml
    from clover_constants import get_clover_home

    home = get_clover_home()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    cron_dir = home / "cron"
    (cron_dir / "output").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(jobs, "CLOVER_DIR", home)
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    job = jobs.create_job("watch CI", "every 1h", name="ci-watch",
                          workdir=str(project), monitor_script="ci-status.sh")
    (home / "config.yaml").write_text(yaml.safe_dump({"proactive": {
        "enabled": True, "eligible_jobs": [job["id"]],
    }}), encoding="utf-8")
    settings = ws.load_settings()
    assert settings.enabled and job["id"] in settings.eligible_jobs

    agent = root_agent([], tmp_path, session_id="octavia")
    agent.platform = "cli"
    scope = ws.scope_for_agent(agent)
    assert scope.private and scope.root == "local"

    db = SessionDB(home / "state.db")
    db.create_session("human-1", source="cli")
    blocker = np.create_workstream(scope, kind="blocker", title="CI red", summary=BLOCKER,
                                   fingerprint="ci-red")
    approved = ws.approve_workstream(
        scope, blocker["workstream_id"], settings=settings, origin=_receipt(db),
        human_reader=origin.read_human_origin, db=db)
    assert approved["approved"]
    linked = ws.link_job_to_workstream(
        scope, blocker["workstream_id"], job["id"], settings, origin=_receipt(db),
        human_reader=origin.read_human_origin, db=db)
    assert linked["workstream_id"] == blocker["workstream_id"]
    yield SimpleNamespace(agent=agent, scope=scope, job=jobs.get_job(job["id"]),
                          blocker=blocker["workstream_id"], settings=settings,
                          project=project, helpers=helpers, db=db)
    db.close()


def _monitor_change(world, monkeypatch, text="CI status: green"):
    """The external monitor source reports a change; everything after is real."""
    monkeypatch.setattr(monitor, "check_monitor", lambda job: monitor.MonitorOutcome(
        ok=True, changed=True, context_block=f"## MONITOR CHANGE DETECTED\n\n{text}"))
    monkeypatch.setattr(jobs, "job_payload_is_empty", lambda _job: False)
    return scheduler.run_job(world.job)


def _drain_release_events():
    from tools.process_registry import process_registry

    out = []
    while not process_registry.completion_queue.empty():
        out.append(process_registry.completion_queue.get_nowait())
    return [e for e in out if e.get("type") == "continuation_release"]


def test_blocker_clear_event_starts_the_held_stage_once_through_admission(
        world, monkeypatch, tmp_path):
    helpers = world.helpers
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    first = _dispatch_stage_one(world.agent, tmp_path, plan)

    # The original runtime bound the stage to the stage before, held on the
    # approved linked blocker and recorded the exact reference.
    bound = ad.get_continuation_plan(first)
    assert bound["hold"]["workstream_id"] == world.blocker
    authority = np.get_authority(world.scope, world.blocker)
    assert authority["plan_ref"]["delegation_id"] == first

    _finish_and_deliver(helpers, first)
    before = set(_all_ids())
    _completion_turn(world.agent, first, "Stage one is done; stage two waits on CI.")
    assert _new_dispatch_ids(before) == [], "the held stage must not start on delivery"
    assert ad.get_continuation_plan(first)["consumed"] == 0

    # A real linked monitor change: evidence only, no model run, no dispatch.
    ok, _doc, final, _err = _monitor_change(world, monkeypatch)
    assert not _new_dispatch_ids(before)
    events = _drain_release_events()
    assert [e["delegation_id"] for e in events] == [first]

    # The gateway injects the trusted internal continuation turn.
    _completion_turn(world.agent, first, "CI is green; stage two is running.")
    started = _new_dispatch_ids(before)
    assert len(started) == 1
    _wait(lambda: len(helpers.goals) == 2, "stage two helper")
    assert STAGE_TWO[:20] in helpers.goals[1]["user"]
    assert ad.get_continuation_plan(first)["consumed"] == 1

    occ = np.list_occurrences(world.scope, world.blocker)
    assert [o["state"] for o in occ] == ["executed"]
    assert started[0] in occ[0]["detail"]
    assert np.get_authority(world.scope, world.blocker)["budget"]["turns"] == 0

    # Replays (event redelivered, restart) start nothing more.
    for _ in range(2):
        _completion_turn(world.agent, first)
    assert len(_new_dispatch_ids(before)) == 1


# ── negative controls ──────────────────────────────────────────────────────


def _held_stage(world, tmp_path, *, deliver=True):
    """Stage one dispatched (and delivered) with the next stage held."""
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    first = _dispatch_stage_one(world.agent, tmp_path, plan)
    assert ad.get_continuation_plan(first)["hold"]["workstream_id"] == world.blocker
    if deliver:
        _finish_and_deliver(world.helpers, first)
        _completion_turn(world.agent, first, "Stage one done; stage two waits on CI.")
    return first


def _occurrence_states(world):
    return [o["state"] for o in np.list_occurrences(world.scope, world.blocker)]


def _release_turn(world, first):
    before = set(_all_ids())
    events = _drain_release_events()
    for _ in events:
        _completion_turn(world.agent, first, "Release turn.")
    return events, _new_dispatch_ids(before)


def test_monitor_change_with_no_bound_plan_settles_unknown_and_dispatches_nothing(
        world, monkeypatch):
    before = set(_all_ids())
    ok, doc, final, err = _monitor_change(world, monkeypatch)
    assert ok is False and "unknown" in doc
    assert _drain_release_events() == []
    assert _new_dispatch_ids(before) == []
    assert _occurrence_states(world) == ["unknown"]


def test_event_before_the_stage_before_settles_waits_then_releases_once(
        world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path, deliver=False)
    before = set(_all_ids())
    _monitor_change(world, monkeypatch)
    assert _drain_release_events() == []
    assert _occurrence_states(world) == ["pending"], "never claimed while unsettled"

    _finish_and_deliver(world.helpers, first)
    _completion_turn(world.agent, first, "Stage one done; stage two waits on CI.")
    assert _new_dispatch_ids(before) == []
    # Restart-style recovery: an unchanged monitor tick consumes the genuinely
    # pending occurrence once.
    monkeypatch.setattr(monitor, "check_monitor", lambda job: monitor.MonitorOutcome(
        ok=True, changed=False))
    scheduler.run_job(world.job)
    scheduler.run_job(world.job)  # a second tick finds nothing pending
    events, started = _release_turn(world, first)
    assert len(events) == 1 and len(started) == 1
    assert _occurrence_states(world) == ["executed"]


def test_same_monitor_change_twice_releases_once(world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    _monitor_change(world, monkeypatch)
    _monitor_change(world, monkeypatch)
    events, started = _release_turn(world, first)
    assert len(events) == 1 and len(started) == 1


def test_a_revoked_plan_is_not_started_and_the_release_is_settled(
        world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    _monitor_change(world, monkeypatch)
    assert ad.revoke_continuation_plan(first)  # an explicit Stop
    events, started = _release_turn(world, first)
    assert len(events) == 1 and started == []
    assert _occurrence_states(world) == ["cancelled"]
    assert ad.get_continuation_plan(first)["consumed"] == 0


def test_stop_pending_at_admission_blocks_the_effect(world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    _monitor_change(world, monkeypatch)
    world.agent._interrupt_requested = True
    try:
        events, started = _release_turn(world, first)
    finally:
        world.agent._interrupt_requested = False
    assert started == []
    # A turn that begins under a pending Stop continues nothing; the claimed
    # release is never executed (and is unknown after a restart, not retried).
    assert _occurrence_states(world) in (["claimed"], ["cancelled"])
    assert ad.get_continuation_plan(first)["consumed"] == 0


def test_exhausted_budget_is_refused(world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    np.spend_budget(world.scope, world.blocker, turns=1)
    _monitor_change(world, monkeypatch)
    events, started = _release_turn(world, first)
    assert started == []
    assert _occurrence_states(world) == ["cancelled"]
    assert ad.get_continuation_plan(first)["consumed"] == 0


def test_changed_scope_is_refused(world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    _monitor_change(world, monkeypatch)
    world.agent._chat_type, world.agent._user_id, world.agent._chat_id = "dm", "42", "42"
    events, started = _release_turn(world, first)
    assert started == []
    assert ad.get_continuation_plan(first)["consumed"] == 0


def test_changed_tool_surface_is_refused(world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    _monitor_change(world, monkeypatch)
    world.agent.enabled_toolsets = list(world.agent.enabled_toolsets or []) + ["terminal"]
    events, started = _release_turn(world, first)
    assert started == []
    assert _occurrence_states(world) == ["cancelled"]


def test_unapproved_or_unlinked_blocker_is_refused(world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    _monitor_change(world, monkeypatch)
    ws.unlink_job(world.job["id"])
    events, started = _release_turn(world, first)
    assert started == []
    assert _occurrence_states(world) == ["cancelled"]


def test_authority_reference_mismatch_is_refused(world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    _monitor_change(world, monkeypatch)
    with np._ws_write() as conn:
        conn.execute("UPDATE work_authority SET plan_ref=?", ('{"delegation_id": "other"}',))
    events, started = _release_turn(world, first)
    assert started == []
    assert ad.get_continuation_plan(first)["consumed"] == 0


def test_another_conversation_cannot_release_the_stage(world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    _monitor_change(world, monkeypatch)
    stranger = root_agent([], tmp_path, session_id="someone-else")
    stranger.platform = "cli"
    before = set(_all_ids())
    for _ in _drain_release_events():
        _completion_turn(stranger, first)
    assert _new_dispatch_ids(before) == []
    assert ad.get_continuation_plan(first)["consumed"] == 0


def test_rejected_worker_start_is_cancelled_not_executed_nor_retried(
        world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    _monitor_change(world, monkeypatch)
    from agent import tool_executor

    real = tool_executor.dispatch_generated_tool
    monkeypatch.setattr(
        tool_executor, "dispatch_generated_tool",
        lambda *a, **k: '{"status": "error", "error": "worker pool refused the start"}')
    events, started = _release_turn(world, first)
    assert started == []
    assert _occurrence_states(world) == ["cancelled"]
    assert ad.get_continuation_plan(first)["consumed"] == 1  # spent, never retried
    assert np.get_authority(world.scope, world.blocker)["budget"]["turns"] == 1  # not charged
    monkeypatch.setattr(tool_executor, "dispatch_generated_tool", real)
    before = set(_all_ids())
    _completion_turn(world.agent, first)  # a redelivery cannot revive it
    assert _new_dispatch_ids(before) == []


def test_crash_after_claim_is_unknown_and_never_retried(world, monkeypatch, tmp_path):
    first = _held_stage(world, tmp_path)
    from agent import continuation_bridge as bridge

    def _crash(_evt):
        raise KeyboardInterrupt("the scheduler process died after the claim")

    real_emit = bridge.emit
    monkeypatch.setattr(bridge, "emit", _crash)
    with pytest.raises(KeyboardInterrupt):
        _monitor_change(world, monkeypatch)
    assert _occurrence_states(world) == ["claimed"]
    monkeypatch.setattr(bridge, "emit", real_emit)

    # A restart proves the claimer gone: the occurrence is unknown, forever.
    changed = np.recover_occurrences(world.scope, owner_alive=lambda _c: False)
    assert [o["state"] for o in changed] == ["unknown"]
    before = set(_all_ids())
    _monitor_change(world, monkeypatch)  # the same change observed again
    events, started = _release_turn(world, first)
    assert events == [] and started == [] and _new_dispatch_ids(before) == []
    assert ad.get_continuation_plan(first)["consumed"] == 0


def test_disabled_mode_binds_no_hold_and_keeps_the_ordinary_chain(
        world, monkeypatch, tmp_path):
    from clover_constants import get_clover_home

    (get_clover_home() / "config.yaml").write_text("proactive:\n  enabled: false\n")
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    first = _dispatch_stage_one(world.agent, tmp_path, plan)
    assert "hold" not in ad.get_continuation_plan(first)
    assert np.get_authority(world.scope, world.blocker) is None
    _finish_and_deliver(world.helpers, first)
    before = set(_all_ids())
    _completion_turn(world.agent, first)
    assert len(_new_dispatch_ids(before)) == 1  # unchanged pre-feature behavior


def test_two_linked_blockers_bind_no_hold(world, monkeypatch, tmp_path):
    import yaml
    from clover_constants import get_clover_home

    second_job = jobs.create_job("watch deploy", "every 1h", name="deploy-watch",
                                 workdir=str(world.project), monitor_script="deploy.sh")
    (get_clover_home() / "config.yaml").write_text(yaml.safe_dump({"proactive": {
        "enabled": True, "eligible_jobs": [world.job["id"], second_job["id"]]}}))
    settings = ws.load_settings()
    other = np.create_workstream(world.scope, kind="blocker", title="deploy",
                                 summary="deploy window closed", fingerprint="deploy-gate")
    ws.approve_workstream(world.scope, other["workstream_id"], settings=settings,
                          origin=_receipt(world.db), human_reader=origin.read_human_origin,
                          db=world.db)
    ws.link_job_to_workstream(world.scope, other["workstream_id"], second_job["id"], settings,
                              origin=_receipt(world.db), human_reader=origin.read_human_origin,
                              db=world.db)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    first = _dispatch_stage_one(world.agent, tmp_path, plan)
    assert "hold" not in ad.get_continuation_plan(first)


# ── the effect is produced by the actual accepted stage executor ───────────

STAGE_CHECK = "Write the read-only release checklist file into the project folder"


@pytest.fixture
def real_stage_executor(monkeypatch, tmp_path, helpers):
    """Stage helpers run the REAL child conversation loop and tool executor;
    only their provider is scripted. Stage one stays the usual scripted
    result; the checklist stage writes a harmless temp file through the real
    ``write_file`` tool."""
    import threading
    from unittest.mock import MagicMock

    import run_agent
    from tests.agent.test_handoff_completion_seam import _call, _text, _tools

    fake = run_agent.AIAgent.run_conversation  # the helpers fixture's wrapper
    # The unpatched conversation loop is the wrapper's own fall-through target.
    real = next(
        cell.cell_contents for cell in fake.__closure__
        if callable(cell.cell_contents)
        and getattr(cell.cell_contents, "__name__", "") == "run_conversation"
    )
    target = tmp_path / "project" / "release-checklist.txt"
    seen = SimpleNamespace(ran=[], effect=target, lock=threading.Lock(), result=None)

    def conversation(self, user_message=None, *args, **kwargs):
        child = getattr(self, "_delegate_depth", 0) or getattr(self, "_subagent_id", None)
        if child and "checklist file" in str(user_message):
            with seen.lock:
                seen.ran.append(str(user_message))
            helpers.gate.wait(20)
            self.client = MagicMock()
            self.client.chat.completions.create.side_effect = [
                _tools(_call("write_file", {"path": str(target), "content": "1. ready\n"})),
                _text("Checklist written."),
            ]
            self._cached_system_prompt = "You are a helper."
            self._use_prompt_caching = False
            self.compression_enabled = False
            self.save_trajectories = False
            self._disable_streaming = True
            out = real(self, user_message, *args, **kwargs)
            seen.result = out
            return out
        return fake(self, user_message, *args, **kwargs)

    monkeypatch.setattr(run_agent.AIAgent, "run_conversation", conversation)
    return seen


def test_released_stage_effect_comes_from_the_accepted_executor(
        world, real_stage_executor, monkeypatch, tmp_path):
    plan = _staged((STAGE_ONE, ()), (STAGE_CHECK, (0,)))
    first = _dispatch_stage_one(world.agent, tmp_path, plan)
    _finish_and_deliver(world.helpers, first)
    before = set(_all_ids())
    _completion_turn(world.agent, first, "Stage one done; the checklist waits on CI.")
    assert _new_dispatch_ids(before) == []
    assert not real_stage_executor.effect.exists(), "nothing executed while held"

    _monitor_change(world, monkeypatch)
    for _ in _drain_release_events():
        _completion_turn(world.agent, first, "CI is green; the checklist stage is running.")
    (second,) = _new_dispatch_ids(before)
    _wait(lambda: real_stage_executor.ran, "the checklist helper to start")
    _wait(lambda: real_stage_executor.result is not None, "the helper conversation")
    _wait(lambda: real_stage_executor.effect.exists(), "the stage executor's effect")
    assert real_stage_executor.effect.read_text() == "1. ready\n"
    # Queued/transport is distinct from effect: the occurrence says dispatched,
    # and the delegation's own row is what confirms completion.
    occ = np.list_occurrences(world.scope, world.blocker)[0]
    assert occ["state"] == "executed" and "effect pending" in occ["detail"]
    _wait(lambda: (ad.get_durable_delegation(second) or {}).get("state") == "completed",
          "the stage delegation to complete")

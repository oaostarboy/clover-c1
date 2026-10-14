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

from agent import delegation_router as dr, router_backstop as rb
from agent import workstream_origin as origin
from agent import workstreams as ws
from cron import jobs, monitor, notepad as np, scheduler
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

"""Owner-authenticated DM metadata through real Telegram ingress and SQLite.

The sole test-only wiring is the documented additive parent prologue hook;
no main admission, SessionDB, gateway watcher or executor is replaced.
"""
import json
from dataclasses import asdict

import pytest

from agent import workstream_human_hooks as hooks, workstream_job_scope as adapter
from agent import workstreams as ws
from cron import jobs, notepad as np
from tests.gateway.test_trusted_human_origin_ingress import Harness, network_guard  # noqa: F401
from tests.agent.test_continuation_bridge import real_stage_executor, STAGE_CHECK  # noqa: F401
from tests.agent.test_router_staged_continuation import helpers, isolated, _all_ids, _wait  # noqa: F401
from tests.agent.test_handoff_completion_seam import _call, _tools, _text
from tests.agent.test_delegation_router import FakeAux, T, verdict_json
import tools.async_delegation as ad

pytestmark = pytest.mark.real_delegation_router


@pytest.fixture
def dm(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=True)
    h.runner._set_session_env = type(h.runner)._set_session_env.__get__(h.runner)
    h.runner._is_user_authorized = type(h.runner)._is_user_authorized.__get__(h.runner)
    for key in ("TELEGRAM_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS"):
        monkeypatch.delenv(key, raising=False)
    async def settled(calls=1, timeout=20):
        import asyncio
        deadline = asyncio.get_running_loop().time() + timeout
        while h.scripted.calls < calls or h.adapter._active_sessions:
            assert asyncio.get_running_loop().time() < deadline, "human turn did not settle"
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)

    h.settle = settled
    real = hooks.consume_persisted_turn
    h.admissions = []
    h.agents = []

    def composed(agent, *, message, row_id):
        result = adapter.consume_persisted_turn(agent, message=message, row_id=row_id)
        h.admissions.append(result)
        h.agents.append(agent)
        return real(agent, message=message, row_id=row_id)

    monkeypatch.setattr(hooks, "consume_persisted_turn", composed)
    scope = ws.scope_for_surface(platform="telegram", chat_type="dm", chat_id="12345", user_id="12345")
    blocker = np.create_workstream(scope, kind="blocker", title="CI gate", summary="wait for CI", fingerprint="ci")
    h.scope, h.blocker = scope, blocker["workstream_id"]
    yield h
    h.close()


def request(h, **changes):
    data = dict(scope=asdict(h.scope), workstream_id=h.blocker, prompt="watch CI",
                schedule="every 1h", monitor_script="ci-status.sh", approve=True)
    data.update(changes)
    return "workstream-monitor: " + json.dumps(data)


def test_authenticated_dm_creation_preserves_the_exact_scope(dm):
    dm.inbound_human(request(dm))
    result = dm.admissions[-1]
    assert result.handled, result.reason
    job = jobs.get_job(result.job_id)
    assert ws.job_scope(job) == dm.scope
    assert dm.scope.audience == "private:12345" and dm.scope.root == "12345"
    assert job["workstream_scope"]["scope"] == asdict(dm.scope)
    assert job["workstream_scope"]["owner"]["session_id"] == dm.session_id()
    assert job["deliver"] == "local"


def enrolled_link(h):
    import yaml

    h.inbound_human(request(h))
    result = h.admissions[-1]
    assert result.handled, result.reason
    jid = result.job_id
    (h.home / "config.yaml").write_text(yaml.safe_dump({"proactive": {
        "enabled": True, "eligible_jobs": [jid]}}))
    h.inbound_human(request(h, job_id=jid), message_id=43)
    assert h.admissions[-1].handled, h.admissions[-1].reason
    job = jobs.get_job(jid)
    assert job["workstream_id"] == h.blocker
    return job


def scripted_stage(h, monkeypatch, tmp_path):
    import agent.auxiliary_client as ac

    fake = FakeAux()
    fake.replies[:] = [verdict_json("staged", [T("Read CI status without changing anything"), T(STAGE_CHECK, [0])])]
    monkeypatch.setattr(ac, "call_llm", fake)
    old = h.scripted.client.chat.completions.create.side_effect
    replies = [_tools(_call("write_file", {"path": str(tmp_path / f"root-{i}.txt"), "content": "x"}))
               for i in range(4)]

    def reply(*a, **kw):
        old(*a, **kw)  # retain actual provider request accounting
        return replies.pop(0) if replies else _text("Reporting only.")

    h.scripted.client.chat.completions.create.side_effect = reply
    h.inbound_human("Build and review a release checklist; wait on the approved CI blocker.", message_id=44)
    (first,) = _all_ids()
    bound = ad.get_continuation_plan(first)
    assert bound["hold"]["scope"] == asdict(h.scope)
    assert bound["hold"]["job_id"] == h.job["id"]
    return first


def watcher_until(h, predicate, timeout=15):
    import asyncio

    async def run():
        h.runner._running = True
        task = asyncio.create_task(h.runner._async_delegation_watcher(interval=0.01))
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while not predicate():
                assert asyncio.get_running_loop().time() < deadline, "gateway watcher did not settle"
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
        finally:
            h.runner._running = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_dm_monitor_real_watcher_releases_the_accepted_executor_once(
        dm, helpers, real_stage_executor, monkeypatch, tmp_path):
    from cron import scheduler

    dm.job = enrolled_link(dm)
    first = scripted_stage(dm, monkeypatch, tmp_path)
    helpers.gate.set()
    _wait(lambda: (ad.get_durable_delegation(first) or {}).get("state") == "completed", "stage one")
    watcher_until(dm, lambda: (ad.get_durable_delegation(first) or {}).get("delivery_state") == "delivered")
    assert len(_all_ids()) == 1 and not real_stage_executor.effect.exists()
    assert ad.get_continuation_plan(first)["consumed"] == 0

    scripts = dm.home / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "ci-status.sh").write_text("printf 'CI green\\n'\n")
    scheduler.run_job(dm.job)  # actual source, hash, canonical scope and occurrence owner
    assert [o["state"] for o in np.list_occurrences(dm.scope, dm.blocker)] == ["claimed"]
    from tools.process_registry import process_registry
    event = process_registry.completion_queue.get_nowait()
    assert event["type"] == "continuation_release" and event["delegation_id"] == first
    process_registry.completion_queue.put(event)
    process_registry.completion_queue.put(dict(event))  # replay the actual producer's event
    watcher_until(dm, lambda: real_stage_executor.effect.exists() and
                  process_registry.completion_queue.empty() and not dm.adapter._active_sessions)
    _wait(lambda: real_stage_executor.result is not None, "accepted stage executor")
    assert real_stage_executor.effect.read_text() == "1. ready\n"
    assert len(_all_ids()) == 2
    assert ad.get_continuation_plan(first)["consumed"] == 1
    assert [o["state"] for o in np.list_occurrences(dm.scope, dm.blocker)] == ["executed"]
    assert np.get_authority(dm.scope, dm.blocker)["budget"]["turns"] == 0
    # The identical unchanged source cannot create or dispatch another occurrence.
    scheduler.run_job(jobs.get_job(dm.job["id"]))
    assert len(np.list_occurrences(dm.scope, dm.blocker)) == 1
    assert len(_all_ids()) == 2
    print(json.dumps({"scope": asdict(dm.scope), "job_id": dm.job["id"],
        "owner": dm.job["workstream_scope"]["owner"], "delegation_id": first,
        "release_event": event, "occurrence": np.list_occurrences(dm.scope, dm.blocker)[0],
        "artifact": str(real_stage_executor.effect), "artifact_content": real_stage_executor.effect.read_text(),
        "stage_result": real_stage_executor.result}, default=str))

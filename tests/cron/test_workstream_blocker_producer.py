"""Normal authenticated producer, not preplanted blocker rows.

The Telegram fixture supplies adapter input and scripts provider/network only.
Both installed post-persistence consumers and the runtime/scheduler are real.
"""
import asyncio
import json
from dataclasses import asdict

import pytest

from agent import workstream_job_scope as admission, workstreams as ws
from cron import jobs, notepad as np
from tests.gateway.test_trusted_human_origin_ingress import Harness, network_guard  # noqa: F401
from tests.agent.test_continuation_bridge import real_stage_executor  # noqa: F401
from tests.agent.test_router_staged_continuation import helpers, isolated, _all_ids, _wait  # noqa: F401
from tests.cron.test_workstream_dm_scope import scripted_stage, watcher_until, ad

pytestmark = pytest.mark.real_delegation_router


@pytest.fixture
def human(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=True)
    # Persist the real project's terminal setting: lazy CLI/config consumers
    # must not replace the accepted scope with the test runner's cwd.
    import yaml
    (h.home / "config.yaml").write_text(yaml.safe_dump({
        "proactive": {"enabled": True}, "terminal": {"cwd": str(h.project)}}))
    h.runner._set_session_env = type(h.runner)._set_session_env.__get__(h.runner)
    h.runner._is_user_authorized = type(h.runner)._is_user_authorized.__get__(h.runner)
    for key in ("TELEGRAM_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS"):
        monkeypatch.delenv(key, raising=False)
    import agent.auxiliary_client as auxiliary
    monkeypatch.setattr(auxiliary, "resolve_provider_client", lambda *a, **k: (None, None))
    monkeypatch.setattr(auxiliary, "get_text_auxiliary_client", lambda *a, **k: (None, None))
    monkeypatch.setattr(auxiliary, "get_async_text_auxiliary_client", lambda *a, **k: (None, None))
    for target in ("agent.model_metadata.fetch_model_metadata", "agent.agent_init.fetch_model_metadata"):
        monkeypatch.setattr(target, lambda *a, **k: {})
    h.admissions, h.agents = [], []
    from run_agent import AIAgent

    def provider(agent, *args, **kwargs):
        h.agents.append(agent)
        return h.scripted.client

    monkeypatch.setattr(AIAgent, "_create_openai_client", provider)

    async def settled(calls=1, timeout=20):
        deadline = asyncio.get_running_loop().time() + timeout
        while h.scripted.calls < calls or h.adapter._active_sessions:
            assert asyncio.get_running_loop().time() < deadline, "human turn did not settle"
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        h.admissions.append(getattr(h.agents[-1], "_workstream_monitor_result", None) or admission.AdmissionResult())

    h.settle = settled
    h.scope = ws.scope_for_surface(platform="telegram", chat_type="dm", chat_id="12345", user_id="12345")
    h.blocker = "ws_abcdef0123456789"  # literal human input, NOT a created row
    assert np.list_workstreams(h.scope) == []
    yield h
    h.close()


def request(h, **changes):
    data = dict(scope=asdict(h.scope), workstream_id=h.blocker,
                blocker={"title": "CI gate", "summary": "Wait for the monitored CI result"},
                prompt="watch CI", schedule="every 1h", monitor_script="ci-status.sh", approve=True)
    data.update(changes)
    return "workstream-monitor: " + json.dumps(data)


def create(h):
    h.inbound_human(request(h))
    result = h.admissions[-1]
    assert result.handled, (result.reason, asdict(h.scope), asdict(ws.scope_for_agent(h.agents[-1])))
    return jobs.get_job(result.job_id)


def test_normal_human_creates_approved_blocker_but_no_execution_authority(human):
    job = create(human)
    blocker = np.get_workstream(human.scope, human.blocker)
    assert blocker["kind"] == "blocker" and blocker["status"] == "open" and blocker["approved"]
    assert blocker["session_id"] == human.session_id()
    assert ws.job_scope(job) == human.scope
    assert not job.get("workstream_id")
    assert ws.load_settings().eligible_jobs == ()
    assert np.get_authority(human.scope, human.blocker) is None
    assert _all_ids() == []
    row = next(r for r in human.user_rows() if r["content"] == request(human))
    import sqlite3
    with sqlite3.connect(human.db_path) as conn:
        receipt = conn.execute("SELECT ingress_event_id, consumed_at FROM human_origin_receipts "
                               "WHERE session_id=? AND message_row_id=?",
                               (human.session_id(), row["id"])).fetchone()
    assert receipt and receipt[1]
    assert blocker["source"] == "human:" + receipt[0]


def enroll(h, job):
    import yaml
    (h.home / "config.yaml").write_text(yaml.safe_dump({
        "proactive": {"enabled": True, "eligible_jobs": [job["id"]]},
        "terminal": {"cwd": str(h.project)}}))
    h.inbound_human(request(h, job_id=job["id"]), message_id=43)
    assert h.admissions[-1].handled, h.admissions[-1].reason
    return jobs.get_job(job["id"])


def test_created_blocker_reaches_original_runtime_and_changed_file_once(
        human, helpers, real_stage_executor, monkeypatch, tmp_path):
    from cron import monitor, scheduler
    from tools.process_registry import process_registry

    job = create(human)
    scripts = human.home / "scripts"
    scripts.mkdir(exist_ok=True)
    source = human.project / "ci-status.txt"
    source.write_text("CI red\n")
    (scripts / "ci-status.sh").write_text("cat ci-status.txt\n")
    # Baseline from the actual file before enrollment/linking, not invented
    # hashes/occurrences. It cannot grant continuation authority.
    assert monitor.check_monitor(job).first_run
    human.job = enroll(human, jobs.get_job(job["id"]))
    first = scripted_stage(human, monkeypatch, tmp_path)
    helpers.gate.set()
    _wait(lambda: (ad.get_durable_delegation(first) or {}).get("state") == "completed", "first stage")
    watcher_until(human, lambda: (ad.get_durable_delegation(first) or {}).get("delivery_state") == "delivered")
    assert len(_all_ids()) == 1 and not real_stage_executor.effect.exists()
    calls = human.scripted.calls
    for _ in range(2):
        scheduler.run_job(jobs.get_job(job["id"]))
    assert human.scripted.calls == calls
    assert np.list_occurrences(human.scope, human.blocker) == []
    assert ad.get_continuation_plan(first)["consumed"] == 0
    source.write_text("CI green\n")  # real monitored file change, no continue
    scheduler.run_job(jobs.get_job(job["id"]))
    assert human.scripted.calls == calls
    event = process_registry.completion_queue.get_nowait()
    assert event["type"] == "continuation_release" and event["delegation_id"] == first
    process_registry.completion_queue.put(event)
    process_registry.completion_queue.put(dict(event))
    watcher_until(human, lambda: real_stage_executor.effect.exists() and
                  process_registry.completion_queue.empty() and not human.adapter._active_sessions)
    _wait(lambda: real_stage_executor.result is not None, "accepted executor result")
    assert real_stage_executor.effect.read_text() == "1. ready\n"
    assert len(real_stage_executor.ran) == 1 and len(_all_ids()) == 2
    assert ad.get_continuation_plan(first)["consumed"] == 1
    assert [o["state"] for o in np.list_occurrences(human.scope, human.blocker)] == ["executed"]
    scheduler.run_job(jobs.get_job(job["id"]))
    assert len(_all_ids()) == 2 and len(real_stage_executor.ran) == 1
    print(json.dumps({"producer": "agent.workstream_job_scope.consume_persisted_turn",
        "scope": asdict(human.scope), "job": job, "blocker": np.get_workstream(human.scope, human.blocker),
        "plan": ad.get_continuation_plan(first), "release_event": event,
        "artifact": str(real_stage_executor.effect), "content": real_stage_executor.effect.read_text()}, default=str))


def test_exact_human_create_request_is_idempotent(human):
    first = create(human)
    before = np.get_workstream(human.scope, human.blocker)
    human.inbound_human(request(human), message_id=45)
    result = human.admissions[-1]
    assert result.handled, result.reason
    assert result.job_id == first["id"]
    assert len(jobs.load_jobs()) == 1
    assert np.get_workstream(human.scope, human.blocker) == before


def test_invalid_schedule_saves_neither_blocker_nor_job(human):
    human.inbound_human(request(human, schedule="not a schedule"))
    assert not human.admissions[-1].handled
    assert np.list_workstreams(human.scope) == []
    assert jobs.load_jobs() == []


def test_changed_create_spec_cannot_adopt_or_rewrite_an_existing_record(human):
    first = create(human)
    before = np.get_workstream(human.scope, human.blocker)
    human.inbound_human(request(human, blocker={"title": "changed", "summary": "other gate"}), message_id=45)
    assert not human.admissions[-1].handled
    assert len(jobs.load_jobs()) == 1 and jobs.get_job(first["id"]) == first
    assert np.get_workstream(human.scope, human.blocker) == before


@pytest.mark.parametrize("after_save", [False, True])
def test_ambiguous_job_creation_is_unknown_and_never_attempted_twice(human, monkeypatch, after_save):
    original = jobs.create_job
    attempts = []

    def broken(*args, **kwargs):
        attempts.append(1)
        if after_save:
            original(*args, **kwargs)
        raise TypeError("scripted ambiguous persistence failure")

    monkeypatch.setattr(jobs, "create_job", broken)
    human.inbound_human(request(human))
    assert not human.admissions[-1].handled
    assert "unknown" in human.admissions[-1].reason
    monkeypatch.setattr(jobs, "create_job", original)
    human.inbound_human(request(human), message_id=45)
    assert not human.admissions[-1].handled
    assert "unknown" in human.admissions[-1].reason
    assert attempts == [1]
    assert len(jobs.load_jobs()) == int(after_save)
    assert np.get_authority(human.scope, human.blocker) is None


@pytest.mark.parametrize("changes", [
    {"blocker": None}, {"blocker": []}, {"blocker": {"title": True, "summary": "wait"}},
    {"blocker": {"title": "CI", "summary": "wait", "approved": True}},
    {"blocker": {"title": " CI", "summary": "wait"}}, {"job_id": None},
    {"workstream_id": "ws_ABCDEF0123456789"}, {"workstream_id": 123},
    {"approve": False}, {"approve": 1}, {"schedule": []}, {"monitor_script": {}},
    {"scope": None}, {"owner": "forged"}, {"blocker": {"title": "CI", "summary": ""}},
])
def test_malformed_explicit_create_is_denied_without_saving(human, changes):
    human.inbound_human(request(human, **changes))
    assert not human.admissions[-1].handled
    assert np.list_workstreams(human.scope) == [] and jobs.load_jobs() == []
    assert _all_ids() == []


@pytest.mark.parametrize("field,value", [
    ("profile", "/foreign/profile"), ("project", "/foreign/project"),
    ("audience", "private"), ("root", "local"),
])
def test_foreign_scope_cannot_create_or_adopt(human, field, value):
    human.inbound_human(request(human, scope={**asdict(human.scope), field: value}))
    assert not human.admissions[-1].handled
    assert np.list_workstreams(human.scope) == [] and jobs.load_jobs() == []


@pytest.mark.parametrize("payload", ["{", "[]", "null", '{"approve":true,"approve":true}'])
def test_invalid_json_is_not_saved(human, payload):
    human.inbound_human("workstream-monitor: " + payload)
    assert not human.admissions[-1].handled
    assert np.list_workstreams(human.scope) == [] and jobs.load_jobs() == []


def test_unenrolled_created_monitor_cannot_link(human):
    job = create(human)
    human.inbound_human(request(human, job_id=job["id"]), message_id=43)
    assert not human.admissions[-1].handled
    assert "independently enrolled" in human.admissions[-1].reason
    assert jobs.get_job(job["id"]) == job
    assert np.get_authority(human.scope, human.blocker) is None


def source_change(h):
    scripts = h.home / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "ci-status.sh").write_text("printf 'CI green\\n'\n")
    from cron import scheduler
    return scheduler.run_job(jobs.get_job(h.job["id"]))


def held(h, helpers, monkeypatch, tmp_path):
    h.job = enroll(h, create(h))
    first = scripted_stage(h, monkeypatch, tmp_path)
    helpers.gate.set()
    _wait(lambda: (ad.get_durable_delegation(first) or {}).get("state") == "completed", "first stage")
    watcher_until(h, lambda: (ad.get_durable_delegation(first) or {}).get("delivery_state") == "delivered")
    return first


def test_created_and_linked_record_without_accepted_stage_never_executes(human):
    human.job = enroll(human, create(human))
    calls = human.scripted.calls
    source_change(human)
    assert human.scripted.calls == calls
    assert np.get_authority(human.scope, human.blocker) is None
    assert [o["state"] for o in np.list_occurrences(human.scope, human.blocker)] == ["unknown"]
    assert _all_ids() == []


@pytest.mark.parametrize("fence", ["revoked", "budget", "expired", "denied_approval", "pending_approval", "stop"])
def test_produced_blocker_keeps_runtime_fences(human, helpers, real_stage_executor, monkeypatch, tmp_path, fence):
    import time
    from clover_cli import plugins
    first = held(human, helpers, monkeypatch, tmp_path)
    source_change(human)
    if fence == "revoked":
        assert ad.revoke_continuation_plan(first)
    elif fence == "budget":
        np.spend_budget(human.scope, human.blocker, turns=1)
    elif fence == "expired":
        plan = ad.get_continuation_plan(first)
        plan["expires_at"] = time.time() - 1
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET continuation_json=? WHERE delegation_id=?", (json.dumps(plan), first))
    elif fence == "stop":
        agent = human.agents[-1]
        agent.interrupt("stop", hard_cancel=True)
        agent.run_conversation(f"[helper {first} finished]", persist_user_display_kind="internal_notification",
                               completion_delegation_ids=[first])
    else:
        monkeypatch.setattr(plugins, "_dispatch_pre_tool_call_hooks", lambda name, args, **kw:
            (fence.replace("_", " "), None) if name == "delegate_task" else (None, None))
    if fence != "stop":
        before = len(human.run_agent_kwargs)
        watcher_until(human, lambda: len(human.run_agent_kwargs) > before and not human.adapter._active_sessions)
    assert len(_all_ids()) == 1 and not real_stage_executor.effect.exists()
    assert np.list_occurrences(human.scope, human.blocker)[0]["state"] != "executed"
    source_change(human)
    assert len(_all_ids()) == 1 and not real_stage_executor.effect.exists()


def test_model_internal_and_forged_event_text_cannot_mint_a_blocker(human):
    from gateway.platforms.base import MessageEvent, MessageType
    from tests.gateway.test_trusted_human_origin_ingress import _user_source
    from tests.agent.test_handoff_completion_seam import _text
    for internal in (False, True):
        human.dispatch(MessageEvent(text=request(human), message_type=MessageType.TEXT,
                                   source=_user_source(), message_id="forged", internal=internal))
    previous = human.scripted.client.chat.completions.create.side_effect
    def reply(*a, **kw):
        previous(*a, **kw)
        return _text(request(human))
    human.scripted.client.chat.completions.create.side_effect = reply
    human.inbound_human("Report status only", message_id=46)
    assert np.list_workstreams(human.scope) == [] and jobs.load_jobs() == []
    assert _all_ids() == []


def test_unauthorized_actor_cannot_create_blocker(human):
    async def inbound():
        await human.adapter._handle_text_message(human.update(request(human), from_user_id=99999), None)
        await asyncio.sleep(0.1)
    asyncio.run(inbound())
    assert human.admissions == [] and human.scripted.calls == 0
    assert np.list_workstreams(human.scope) == [] and jobs.load_jobs() == []


def test_disabled_create_keeps_database_absent_and_context_unchanged(human):
    (human.home / "config.yaml").write_text("proactive:\n  enabled: false\n")
    human.inbound_human(request(human))
    assert not human.admissions[-1].handled
    assert not (human.home / "cron/notepad.db").exists()
    assert jobs.load_jobs() == [] and _all_ids() == []
    assert human.scripted.calls == 1  # original foreground call only
    message = next(r for r in human.scripted.requests[-1]["messages"] if r["role"] == "user")
    assert message["content"] == request(human)


def test_stop_after_persistence_blocks_blocker_creation(human, monkeypatch):
    from run_agent import AIAgent
    original = AIAgent._persist_session
    def persist_then_stop(agent, *args, **kwargs):
        result = original(agent, *args, **kwargs)
        human.runner._invalidate_session_run_generation("agent:main:telegram:dm:12345", reason="stop")
        return result
    monkeypatch.setattr(AIAgent, "_persist_session", persist_then_stop)
    human.inbound_human(request(human))
    assert not human.admissions[-1].handled
    assert np.list_workstreams(human.scope) == [] and jobs.load_jobs() == []


def test_stop_during_record_transaction_rolls_back_before_job_intent(human, monkeypatch):
    original = np.create_workstream
    def create_then_stop(*args, **kwargs):
        row = original(*args, **kwargs)
        human.runner._invalidate_session_run_generation("agent:main:telegram:dm:12345", reason="stop")
        return row
    monkeypatch.setattr(np, "create_workstream", create_then_stop)
    human.inbound_human(request(human))
    assert not human.admissions[-1].handled
    assert np.list_workstreams(human.scope) == [] and jobs.load_jobs() == []


def test_record_approval_failure_rolls_back_creation(human, monkeypatch):
    monkeypatch.setattr(ws, "approve_workstream", lambda *args, **kwargs: None)
    human.inbound_human(request(human))
    assert not human.admissions[-1].handled
    assert "unknown" in human.admissions[-1].reason
    assert np.list_workstreams(human.scope) == [] and jobs.load_jobs() == []


def test_tool_owner_request_and_task_prose_cannot_create_blockers(human):
    human.inbound_human("Please create and approve a CI blocker with the cronjob tool")
    assert np.list_workstreams(human.scope) == []
    row = human.user_rows()[0]
    with pytest.raises(ValueError, match="authenticated"):
        with jobs._use_workstream_scope_owner(human.agents[-1], message=request(human), row_id=row["id"]):
            pytest.fail("tool fabricated the human owner")
    assert jobs.load_jobs() == []


def test_save_receipt_failure_never_retries_an_ambiguous_job(human, monkeypatch):
    original = np.add_receipt
    def receipt(*args, **kwargs):
        if kwargs["kind"] == "saved":
            raise TypeError("scripted local receipt failure after jobs.json commit")
        return original(*args, **kwargs)
    monkeypatch.setattr(np, "add_receipt", receipt)
    human.inbound_human(request(human))
    assert not human.admissions[-1].handled and "unknown" in human.admissions[-1].reason
    job = jobs.load_jobs()[0]
    monkeypatch.setattr(np, "add_receipt", original)
    human.inbound_human(request(human), message_id=45)
    assert not human.admissions[-1].handled and "unknown" in human.admissions[-1].reason
    assert jobs.load_jobs() == [job]
    human.job = enroll_after_unknown = job
    import yaml
    (human.home / "config.yaml").write_text(yaml.safe_dump({
        "proactive": {"enabled": True, "eligible_jobs": [job["id"]]},
        "terminal": {"cwd": str(human.project)}}))
    human.inbound_human(request(human, job_id=job["id"]), message_id=46)
    assert not human.admissions[-1].handled and "unknown" in human.admissions[-1].reason
    assert jobs.get_job(job["id"]) == enroll_after_unknown


@pytest.mark.parametrize("field", ["title", "summary"])
def test_secret_blocker_text_is_denied_before_ledger_write(human, field):
    spec = {"title": "CI gate", "summary": "Wait for CI"}
    spec[field] = "token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    human.inbound_human(request(human, blocker=spec))
    assert not human.admissions[-1].handled
    assert np.list_workstreams(human.scope) == [] and jobs.load_jobs() == []


def test_two_authenticated_actors_cannot_adopt_the_others_scope(human):
    first = create(human)
    human.adapter.config.extra["allow_from"] = ["12345", "54321"]
    second_scope = ws.scope_for_surface(platform="telegram", chat_type="dm", chat_id="54321", user_id="54321")
    async def inbound(text, mid):
        update = human.update(text, from_user_id=54321, message_id=mid)
        update.message.chat.id = 54321
        expected = human.scripted.calls + 1
        await human.adapter._handle_text_message(update, None)
        await human.settle(calls=expected)
    asyncio.run(inbound(request(human), 52))
    assert not human.admissions[-1].handled  # original actor's scope
    assert len(jobs.load_jobs()) == 1
    asyncio.run(inbound(request(human, scope=asdict(second_scope)), 53))
    assert human.admissions[-1].handled, human.admissions[-1].reason
    second_job = jobs.get_job(human.admissions[-1].job_id)
    assert ws.job_scope(second_job) == second_scope and second_scope != human.scope
    assert np.get_workstream(second_scope, human.blocker)["approved"]
    assert jobs.get_job(first["id"]) == first
    asyncio.run(inbound(request(human, scope=asdict(second_scope), job_id=first["id"]), 54))
    assert not human.admissions[-1].handled
    assert jobs.get_job(first["id"]) == first


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="forked production-capability race is Linux-specific")
@pytest.mark.parametrize("phase", ["create", "link"])
def test_two_processes_race_the_actual_authenticated_consumer(human, monkeypatch, phase):
    """Fork only after the real adapter's current user row is authenticated.

    Both workers inherit that actual capability, reauthenticate the real row,
    and enter the unchanged installed consumer. No receipt/ID is fabricated.
    Fork occurs before notepad transactions; each worker opens its own SQLite
    transaction and the existing jobs file lock remains the external owner.
    """
    import os
    import multiprocessing
    from agent import workstream_human_hooks as hooks

    job = create(human) if phase == "link" else None
    if job:
        import yaml
        (human.home / "config.yaml").write_text(yaml.safe_dump({
            "proactive": {"enabled": True, "eligible_jobs": [job["id"]]},
            "terminal": {"cwd": str(human.project)}}))
    text = request(human, **({"job_id": job["id"]} if job else {}))
    original = hooks._authenticate
    parent = os.getpid()
    barrier = multiprocessing.get_context("fork").Barrier(2, timeout=15)
    started = False
    synchronized = False
    child_pid = None
    read_fd, write_fd = os.pipe()

    def authenticated(agent, **kwargs):
        nonlocal started, synchronized, child_pid
        result = original(agent, **kwargs)
        if result is None or kwargs["message"] != text:
            return result
        if os.getpid() != parent and not synchronized:
            synchronized = True
            barrier.wait()
        elif not started:
            started = True
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    outcome = admission.consume_persisted_turn(agent, message=text, row_id=kwargs["row_id"])
                    os.write(write_fd, json.dumps(asdict(outcome)).encode())
                    os._exit(0)
                except BaseException as exc:
                    os.write(write_fd, json.dumps({"error": repr(exc)}).encode())
                    os._exit(1)
            barrier.wait()
        return result

    monkeypatch.setattr(hooks, "_authenticate", authenticated)
    try:
        human.inbound_human(text, message_id=51)
        assert child_pid
        _pid, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        child_result = json.loads(os.read(read_fd, 8192))
        assert "error" not in child_result
        assert child_result["handled"] or human.admissions[-1].handled
        assert len(jobs.load_jobs()) == 1
        blocker = np.get_workstream(human.scope, human.blocker)
        assert blocker["approved"] and blocker["revision"] == 2
        assert [r["revision"] for r in np.list_history(human.scope, human.blocker)] == [1, 2]
        if phase == "link":
            assert jobs.get_job(job["id"])["workstream_id"] == human.blocker
        else:
            assert not jobs.load_jobs()[0].get("workstream_id")
        assert np.get_authority(human.scope, human.blocker) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)

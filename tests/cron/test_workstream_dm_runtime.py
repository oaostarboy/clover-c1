"""Fail-closed controls on the same real authenticated DM admission path."""
import asyncio
import json
import sqlite3
import time
from dataclasses import asdict

import pytest

from agent import workstream_job_scope as adapter, workstreams as ws
from cron import jobs, notepad as np, scheduler
from tests.cron.test_workstream_dm_scope import (  # noqa: F401
    dm, network_guard, helpers, isolated, real_stage_executor,
    request, enrolled_link, scripted_stage, watcher_until, _all_ids, _wait, ad, _text)

pytestmark = pytest.mark.real_delegation_router


@pytest.mark.parametrize("field,value", [
    ("profile", "/foreign/profile"), ("project", "/foreign/project"),
    ("audience", "private:54321"), ("root", "54321"),
])
def test_original_scope_mismatch_never_creates_a_job_or_approves(dm, field, value):
    dm.inbound_human(request(dm, scope={**asdict(dm.scope), field: value}))
    assert not dm.admissions[-1].handled
    assert jobs.list_jobs(include_disabled=True) == []
    assert not np.get_workstream(dm.scope, dm.blocker)["approved"]
    assert _all_ids() == []


def test_ordinary_origin_fields_and_forged_owner_payload_do_not_set_dm_scope(dm):
    legacy = jobs.create_job("watch CI", "every 1h", workdir=str(dm.project),
                             monitor_script="ci-status.sh", origin={
                                 "audience": "private:12345", "user_id": "12345",
                                 "workstream_scope": asdict(dm.scope)})
    assert ws.job_scope(legacy).audience == "private"
    assert ws.job_scope(legacy).root == ""
    with pytest.raises(ValueError, match="cannot be updated"):
        jobs.update_job(legacy["id"], {"workstream_scope": {"scope": asdict(dm.scope)}})
    forged = {**legacy, "workstream_scope": {"version": 1, "job_id": legacy["id"],
        "scope": asdict(dm.scope), "owner": {"session_id": "fake", "message_row_id": 1,
        "ingress_event_id": "fake"}}, "workstream_root": dm.scope.root}
    with pytest.raises(ValueError, match="persisted"):
        ws.job_scope(forged)
    dm.inbound_human(request(dm, job_id=legacy["id"]))
    assert not dm.admissions[-1].handled
    assert not jobs.get_job(legacy["id"]).get("workstream_id")


def test_model_text_fake_event_and_old_human_row_cannot_create_or_reauthorize(dm):
    from gateway.platforms.base import MessageEvent, MessageType
    from tests.gateway.test_trusted_human_origin_ingress import _user_source

    text = request(dm)
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=_user_source(),
                         message_id="forged", internal=False)
    dm.dispatch(event)
    assert jobs.list_jobs(include_disabled=True) == []
    dm.inbound_human(text, message_id=45)
    assert dm.admissions[-1].handled
    agent = dm.agents[-1]
    row = next(r for r in reversed(dm.user_rows()) if r["content"] == text)
    before = jobs.load_jobs()
    assert not adapter.consume_persisted_turn(agent, message=text, row_id=row["id"]).handled
    with pytest.raises(ValueError, match="authenticated"):
        with jobs._use_workstream_scope_owner(agent, message=text, row_id=row["id"]):
            pytest.fail("historical source reauthorized")
    assert jobs.load_jobs() == before
    old = dm.scripted.client.chat.completions.create.side_effect
    def model(*a, **kw):
        old(*a, **kw)
        return _text(text)
    dm.scripted.client.chat.completions.create.side_effect = model
    dm.inbound_human("Please report status, do not create a job", message_id=46)
    assert jobs.load_jobs() == before


def test_unenrolled_link_and_foreign_job_do_not_modify_owned_scope(dm):
    dm.inbound_human(request(dm))
    jid = dm.admissions[-1].job_id
    before = jobs.get_job(jid)
    dm.inbound_human(request(dm, job_id=jid), message_id=47)
    assert not dm.admissions[-1].handled
    assert jobs.get_job(jid) == before
    dm.inbound_human(request(dm, job_id="foreign-job"), message_id=48)
    assert not dm.admissions[-1].handled
    assert jobs.get_job(jid) == before


def test_two_real_private_dms_same_project_do_not_share_scope_or_owner(dm):
    first = enrolled_link(dm)
    second_scope = ws.scope_for_surface(platform="telegram", chat_type="dm",
                                        chat_id="54321", user_id="54321")
    assert second_scope.project == dm.scope.project
    second = np.create_workstream(second_scope, kind="blocker", title="other CI",
                                  summary="other person's gate", fingerprint="other-ci")
    dm.adapter.config.extra["allow_from"] = ["12345", "54321"]
    update = dm.update(request(dm, scope=asdict(second_scope),
                                workstream_id=second["workstream_id"]),
                       message_id=49, from_user_id=54321)
    update.message.chat.id = 54321
    async def inbound(update):
        expected = dm.scripted.calls + 1
        await dm.adapter._handle_text_message(update, None)
        await dm.settle(calls=expected)
    asyncio.run(inbound(update))
    assert dm.admissions[-1].handled, dm.admissions[-1].reason
    second_job = jobs.get_job(dm.admissions[-1].job_id)
    assert ws.job_scope(first) != ws.job_scope(second_job)
    assert ws.job_scope(second_job) == second_scope
    foreign = dm.update(request(dm, scope=asdict(second_scope), job_id=first["id"],
                                 workstream_id=second["workstream_id"]),
                        message_id=50, from_user_id=54321)
    foreign.message.chat.id = 54321
    asyncio.run(inbound(foreign))
    assert not dm.admissions[-1].handled
    assert jobs.get_job(first["id"]) == first
    assert not jobs.get_job(second_job["id"]).get("workstream_id")


def release_source(h):
    scripts = h.home / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "ci-status.sh").write_text("printf 'CI green\\n'\n")
    return scheduler.run_job(h.job)


def held_dm(h, helpers, monkeypatch, tmp_path):
    h.job = enrolled_link(h)
    first = scripted_stage(h, monkeypatch, tmp_path)
    helpers.gate.set()
    _wait(lambda: (ad.get_durable_delegation(first) or {}).get("state") == "completed", "stage one")
    watcher_until(h, lambda: (ad.get_durable_delegation(first) or {}).get("delivery_state") == "delivered")
    assert len(_all_ids()) == 1
    return first


@pytest.mark.parametrize("fence", ["revoked", "budget", "digest", "generation", "expired",
                                   "denied_approval", "pending_approval"])
def test_dm_release_keeps_original_runtime_fences(
        dm, helpers, real_stage_executor, monkeypatch, tmp_path, fence):
    from clover_cli import plugins

    first = held_dm(dm, helpers, monkeypatch, tmp_path)
    release_source(dm)
    assert np.list_occurrences(dm.scope, dm.blocker)[0]["state"] == "claimed"
    if fence == "revoked":
        assert ad.revoke_continuation_plan(first)
    elif fence == "budget":
        np.spend_budget(dm.scope, dm.blocker, turns=1)
    elif fence in ("digest", "generation", "expired"):
        plan = ad.get_continuation_plan(first)
        if fence == "digest":
            plan["digests"][0] = "wrong"
        elif fence == "generation":
            plan["generation"] += 1
        else:
            plan["expires_at"] = time.time() - 1
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET continuation_json=? WHERE delegation_id=?",
                         (json.dumps(plan), first))
    else:
        monkeypatch.setattr(plugins, "_dispatch_pre_tool_call_hooks", lambda name, args, **kw:
            (fence.replace("_", " "), None) if name == "delegate_task" else (None, None))
    before = len(dm.run_agent_kwargs)
    watcher_until(dm, lambda: len(dm.run_agent_kwargs) > before and not dm.adapter._active_sessions)
    assert len(_all_ids()) == 1
    assert not real_stage_executor.effect.exists()
    occurrence = np.list_occurrences(dm.scope, dm.blocker)[0]
    assert occurrence["state"] in ("cancelled", "claimed")
    if occurrence["state"] == "claimed":
        assert np.recover_occurrences(dm.scope, owner_alive=lambda _: False)[0]["state"] == "unknown"
    release_source(dm)
    assert len(_all_ids()) == 1


def test_dm_link_without_an_accepted_stage_has_no_execution_authority(dm):
    dm.job = enrolled_link(dm)
    assert np.get_authority(dm.scope, dm.blocker) is None
    release_source(dm)
    assert np.list_occurrences(dm.scope, dm.blocker)[0]["state"] == "unknown"
    assert _all_ids() == []


def test_disabled_adapter_writes_no_job_or_workstream_and_injects_no_context(dm):
    def counts():
        with sqlite3.connect(dm.home / "cron" / "notepad.db") as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    before = counts()
    (dm.home / "config.yaml").write_text("proactive:\n  enabled: false\n")
    dm.inbound_human(request(dm))
    assert not dm.admissions[-1].handled
    assert jobs.list_jobs(include_disabled=True) == []
    assert counts() == before
    assert dm.scripted.calls == 1
    assert not any("wait for CI" in str(r.get("content")) for r in dm.scripted.requests[-1]["messages"])


@pytest.mark.parametrize("changes", [{"job_id": None}, {"job_id": " padded "},
                                    {"scope": {}}, {"scope": None}, {"approve": False},
                                    {"owner": "forged"}])
def test_missing_or_malformed_scope_and_owner_never_infer_from_last_caller(dm, changes):
    dm.inbound_human(request(dm, **changes))
    assert not dm.admissions[-1].handled
    assert jobs.list_jobs(include_disabled=True) == []
    assert not np.get_workstream(dm.scope, dm.blocker)["approved"]


def test_duplicate_literal_scope_fields_are_not_normalized(dm):
    text = request(dm).replace('"root": "12345"', '"root": "foreign", "root": "12345"')
    dm.inbound_human(text)
    assert not dm.admissions[-1].handled
    assert jobs.list_jobs(include_disabled=True) == []


def test_unauthorized_actual_actor_never_reaches_owner_admission(dm):
    async def inbound():
        await dm.adapter._handle_text_message(dm.update(request(dm), from_user_id=99999), None)
        await asyncio.sleep(0.1)
    asyncio.run(inbound())
    assert dm.admissions == []
    assert dm.scripted.calls == 0
    assert jobs.list_jobs(include_disabled=True) == []


def test_pending_stop_uses_real_agent_interrupt_and_never_executes(
        dm, helpers, real_stage_executor, monkeypatch, tmp_path):
    first = held_dm(dm, helpers, monkeypatch, tmp_path)
    release_source(dm)
    agent = dm.agents[-1]
    agent.interrupt("stop", hard_cancel=True)
    agent.run_conversation(
        f"[helper {first} finished]", persist_user_display_kind="internal_notification",
        completion_delegation_ids=[first],
    )
    assert len(_all_ids()) == 1
    assert not real_stage_executor.effect.exists()
    assert np.list_occurrences(dm.scope, dm.blocker)[0]["state"] != "executed"


@pytest.mark.parametrize("field,value", [("workstream_root", "foreign"), ("workdir", None)])
def test_changed_owner_job_scope_cannot_broaden_or_release(
        dm, helpers, real_stage_executor, monkeypatch, tmp_path, field, value):
    first = held_dm(dm, helpers, monkeypatch, tmp_path)
    if field == "workstream_root":
        before = jobs.get_job(dm.job["id"])
        with pytest.raises(ValueError, match="accepted job root"):
            jobs.update_job(dm.job["id"], {field: value})
        assert jobs.get_job(dm.job["id"]) == before
        job = {**before, field: value}
    else:
        job = jobs.update_job(dm.job["id"], {field: value})
    with pytest.raises(ValueError):
        ws.job_scope(job)
    from cron.monitor import MonitorOutcome
    assert scheduler._record_workstream_monitor_occurrence(
        job, MonitorOutcome(ok=True, changed=True, context_block="changed")) is None
    assert ad.get_continuation_plan(first)["consumed"] == 0
    assert len(_all_ids()) == 1 and not real_stage_executor.effect.exists()


def test_old_unknown_source_is_not_reauthorized_by_a_later_accepted_plan(
        dm, helpers, real_stage_executor, monkeypatch, tmp_path):
    dm.job = enrolled_link(dm)
    release_source(dm)
    assert np.list_occurrences(dm.scope, dm.blocker)[0]["state"] == "unknown"
    first = scripted_stage(dm, monkeypatch, tmp_path)
    helpers.gate.set()
    _wait(lambda: (ad.get_durable_delegation(first) or {}).get("state") == "completed", "stage one")
    watcher_until(dm, lambda: (ad.get_durable_delegation(first) or {}).get("delivery_state") == "delivered")
    dm.job = jobs.get_job(dm.job["id"])
    release_source(dm)  # actual unchanged hash: no newly approved event
    assert [o["state"] for o in np.list_occurrences(dm.scope, dm.blocker)] == ["unknown"]
    assert ad.get_continuation_plan(first)["consumed"] == 0
    assert len(_all_ids()) == 1 and not real_stage_executor.effect.exists()


def test_foreign_monitor_job_cannot_claim_the_original_jobs_occurrence(
        dm, helpers, real_stage_executor, monkeypatch, tmp_path):
    import yaml
    from agent import continuation_bridge

    first = held_dm(dm, helpers, monkeypatch, tmp_path)
    original = dm.job
    dm.inbound_human(request(dm), message_id=55)
    foreign_id = dm.admissions[-1].job_id
    assert foreign_id and foreign_id != original["id"]
    (dm.home / "config.yaml").write_text(yaml.safe_dump({"proactive": {
        "enabled": True, "eligible_jobs": [original["id"], foreign_id]}}))
    with pytest.raises(ValueError, match="authenticated owner"):
        jobs.update_job(foreign_id, {"workstream_id": dm.blocker})
    assert not jobs.get_job(foreign_id).get("workstream_id")
    dm.inbound_human(request(dm, job_id=foreign_id), message_id=56)
    assert dm.admissions[-1].handled, dm.admissions[-1].reason
    foreign = jobs.get_job(foreign_id)
    assert continuation_bridge.held_route(foreign) == ("none", None)
    dm.job = foreign
    release_source(dm)
    assert [o["state"] for o in np.list_occurrences(dm.scope, dm.blocker)] == ["unknown"]
    assert ad.get_continuation_plan(first)["consumed"] == 0
    assert len(_all_ids()) == 1 and not real_stage_executor.effect.exists()

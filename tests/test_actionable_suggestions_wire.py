"""Real PTB serialization and callback -> existing runtime -> exact owner readback."""
import asyncio
import json

import pytest
from telegram import Update
from telegram.ext import Application

from cron import jobs, notepad
from tests.test_actionable_suggestions import make_diagnostic
from tests.gateway.test_workstream_suggestion_offer import network_guard  # noqa: F401
from tests.test_owner_telegram_wire import TelegramEndpoint, TOKEN
from tests.gateway.test_workstream_suggestion_offer import scope


@pytest.fixture
def native(monkeypatch, tmp_path):
    endpoint = TelegramEndpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    asyncio.run(app.initialize())

    def setup(h):
        from gateway.config import Platform

        h.runner.config.platforms[Platform.TELEGRAM] = h.adapter.config
        h.adapter._bot = app.bot
        h.adapter._register_handlers(app)

    h, target = make_diagnostic(monkeypatch, tmp_path, setup)
    send = [p for m, p in endpoint.calls if m == "sendMessage"][-1]
    app.bot_data["action_choices"] = [b["callback_data"] for b in send["reply_markup"]["inline_keyboard"][0]]
    yield h, target, app, endpoint
    asyncio.run(app.shutdown())
    h.close()


def native_tap(app, data="cp:0", *, actor=12345, message=50, chat=12345, thread=None):
    if data in {"cp:0", "cp:1"}:
        data = app.bot_data["action_choices"][int(data[-1])]
    msg = {"message_id": message, "date": 1800000000,
           "chat": {"id": chat, "type": "private"}, "text": "proposal"}
    if thread is not None:
        msg["message_thread_id"] = thread
    return Update.de_json({"update_id": 100, "callback_query": {
        "id": "test-callback", "from": {"id": actor, "is_bot": False, "first_name": "Ant"},
        "chat_instance": "test", "data": data, "message": msg}}, app.bot)


def test_native_one_tap_runs_exact_bounded_action_and_reads_it_back(native):
    h, target, app, endpoint = native
    artifact = h.results[-1]["workstream_suggestion"]
    # Production adapter delivery callback sends this, not a test helper.
    sends = [p for m, p in endpoint.calls if m == "sendMessage"]
    assert len(sends) == 1
    buttons = sends[-1]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["🔧 Do it", "Not now"]
    assert target in sends[-1]["text"] and len(sends[-1]["text"]) < 240
    assert jobs.get_job(target)["enabled"] is True
    prior_calls = h.scripted.calls
    asyncio.run(app.process_update(native_tap(app, buttons[0]["callback_data"])))
    assert jobs.get_job(target)["enabled"] is False
    assert jobs.get_job(target)["state"] == "paused"
    edits = [p for m, p in endpoint.calls if m == "editMessageText"]
    assert "Verified" in edits[-1]["text"]
    assert "fixed" not in edits[-1]["text"].lower()
    assert h.scripted.calls == prior_calls
    receipts = notepad.list_receipts(scope(h), artifact["workstream_id"])
    assert any(r["kind"] == "executed" and r["owner"] == "actionable_suggestion" for r in receipts)
    assert any('"state":"verified"' in r["detail"] for r in receipts)
    asyncio.run(app.process_update(native_tap(app, buttons[0]["callback_data"])))
    assert jobs.get_job(target)["enabled"] is False


@pytest.mark.parametrize("bad", ["actor", "chat", "message", "thread", "expired", "stop",
                                     "revoke", "credential", "profile", "policy", "budget",
                                     "target", "job-active", "job-unknown", "tool-revoked", "config-tool"])
def test_stale_foreign_unsafe_or_revoked_taps_have_no_effect(native, monkeypatch, tmp_path, bad):
    from agent import actionable_suggestions as actions
    from gateway.config import Platform
    from gateway import actionable_suggestions as surface

    h, target, app, endpoint = native
    artifact = h.results[-1]["workstream_suggestion"]
    p = actions.pending(artifact["action"]["token"])
    assert p is not None
    kwargs = {}
    if bad == "actor": kwargs["actor"] = 9999
    if bad == "chat": kwargs["chat"] = 9999
    if bad == "message": kwargs["message"] = 49
    if bad == "thread": kwargs["thread"] = 11
    if bad == "expired": p.expires_at = 0
    if bad == "stop": p.agent._interrupt_requested = True
    if bad == "revoke": h.runner.config.platforms[Platform.TELEGRAM].extra["allow_admin_from"] = ["other"]
    if bad == "credential": h.adapter.config.token = "changed-credential"
    if bad == "profile": h.runner._resolve_profile_home_for_source = lambda source: tmp_path / "foreign"
    if bad == "policy": monkeypatch.setattr(surface, "_strict_policy", lambda: {"proactive": {"enabled": False}})
    if bad == "budget":
        from agent.iteration_budget import IterationBudget

        p.agent.iteration_budget = IterationBudget(0)
    if bad == "target": jobs.update_job(target, {"prompt": "changed exact target"})
    if bad == "job-active": jobs.update_job(target, {"run_claim": {"owner": "synthetic active run"}})
    if bad == "job-unknown": monkeypatch.setattr(jobs, "get_job", lambda job_id: None)
    if bad == "tool-revoked": p.agent.valid_tool_names = []
    if bad == "config-tool":
        import yaml

        config = yaml.safe_load((h.home / "config.yaml").read_text())
        config["agent"] = {"disabled_toolsets": ["cronjob"]}
        (h.home / "config.yaml").write_text(yaml.safe_dump(config))
    before = jobs.load_jobs()
    calls = h.scripted.calls
    asyncio.run(app.process_update(native_tap(app, **kwargs)))
    assert jobs.load_jobs() == before
    assert h.scripted.calls == calls
    receipts = notepad.list_receipts(scope(h), artifact["workstream_id"])
    assert not any(r["kind"] == "executed" for r in receipts)
    assert not any('"state":"verified"' in r["detail"] for r in receipts)


def test_not_now_records_existing_feedback_without_execution(native):
    h, target, app, endpoint = native
    artifact = h.results[-1]["workstream_suggestion"]
    asyncio.run(app.process_update(native_tap(app, "cp:1")))
    assert jobs.get_job(target)["enabled"] is True
    assert notepad.list_feedback(scope(h), artifact["workstream_id"])[-1]["kind"] == "defer"
    assert not any(r["kind"] == "authorized" for r in notepad.list_receipts(scope(h), artifact["workstream_id"]))


def test_concurrent_double_tap_has_one_execution_receipt(native):
    h, target, app, endpoint = native

    async def tap_twice():
        await asyncio.gather(app.process_update(native_tap(app)), app.process_update(native_tap(app)))

    asyncio.run(tap_twice())
    rows = notepad.list_receipts(scope(h), h.results[-1]["workstream_suggestion"]["workstream_id"])
    assert len([r for r in rows if r["kind"] == "executed"]) == 1
    assert jobs.get_job(target)["enabled"] is False


def test_runtime_fabricated_success_is_not_execution_or_verification(native, monkeypatch):
    from agent import tool_executor
    h, target, app, endpoint = native
    monkeypatch.setattr(tool_executor, "dispatch_generated_tool", lambda *a, **kw: '{"success":true}')
    asyncio.run(app.process_update(native_tap(app)))
    rows = notepad.list_receipts(scope(h), h.results[-1]["workstream_suggestion"]["workstream_id"])
    assert not any(r["kind"] == "executed" for r in rows), "Only the job owner can prove execution"
    assert jobs.get_job(target)["enabled"] is True


@pytest.mark.parametrize("action", ["remove", "run", "create", "resume", "send", "spend", "delete"])
def test_middleware_cannot_broaden_approved_action(native, monkeypatch, action):
    from clover_cli import plugins

    h, target, app, endpoint = native
    before = jobs.load_jobs()
    monkeypatch.setattr(plugins, "_dispatch_pre_tool_call_hooks", lambda name, args, **kw:
                        (None, {"action": action, "job_id": target}))
    asyncio.run(app.process_update(native_tap(app)))
    assert jobs.load_jobs() == before
    rows = notepad.list_receipts(scope(h), h.results[-1]["workstream_suggestion"]["workstream_id"])
    assert not any(r["kind"] == "executed" for r in rows)


def test_stop_during_runtime_middleware_wins_before_job_mutation(native, monkeypatch):
    from agent import actionable_suggestions as actions
    from clover_cli import plugins

    h, target, app, endpoint = native
    p = actions.pending(h.results[-1]["workstream_suggestion"]["action"]["token"])

    def stop(name, args, **kw):
        p.agent._interrupt_requested = True
        return None, None

    monkeypatch.setattr(plugins, "_dispatch_pre_tool_call_hooks", stop)
    asyncio.run(app.process_update(native_tap(app)))
    assert jobs.get_job(target)["enabled"] is True


def test_readback_unknown_never_reports_verified_or_retries(native, monkeypatch):
    from agent import actionable_suggestions as actions

    h, target, app, endpoint = native
    real = actions._receipt

    def lose_readback(p, kind, state, **kwargs):
        real(p, kind, state, **kwargs)
        if state == "completed":
            monkeypatch.setattr(jobs, "get_job", lambda job_id: None)

    monkeypatch.setattr(actions, "_receipt", lose_readback)
    asyncio.run(app.process_update(native_tap(app)))
    rows = notepad.list_receipts(scope(h), h.results[-1]["workstream_suggestion"]["workstream_id"])
    assert any(r["kind"] == "executed" for r in rows)
    assert not any('"state":"verified"' in r["detail"] for r in rows)
    edits = [p for m, p in endpoint.calls if m == "editMessageText"]
    assert "verification is unknown" in edits[-1]["text"]
    assert any('"state":"verification_unknown"' in r["detail"] for r in rows)
    assert jobs.load_jobs()[0]["enabled"] is False


def test_job_save_failure_preserves_failed_not_verified_outcome(native, monkeypatch):
    h, target, app, endpoint = native
    before = jobs.load_jobs()

    def fail(jobs):
        raise OSError("synthetic offline write failure")

    monkeypatch.setattr(jobs, "save_jobs", fail)
    asyncio.run(app.process_update(native_tap(app)))
    assert jobs.load_jobs() == before
    rows = notepad.list_receipts(scope(h), h.results[-1]["workstream_suggestion"]["workstream_id"])
    assert any('"state":"failed"' in r["detail"] for r in rows)
    assert not any(r["kind"] == "executed" for r in rows)


@pytest.mark.parametrize("policy", ["[invalid]", "broken: [", "false", "0", "[]"])
def test_real_managed_invalid_policy_cannot_authorize_callback(native, monkeypatch, tmp_path, policy):
    from clover_cli import managed_scope

    h, target, app, endpoint = native
    directory = tmp_path / "managed"
    directory.mkdir()
    (directory / "config.yaml").write_text(policy)
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: directory)
    before = jobs.load_jobs()
    asyncio.run(app.process_update(native_tap(app)))
    assert jobs.load_jobs() == before
    assert not any(r["kind"] == "executed" for r in notepad.list_receipts(
        scope(h), h.results[-1]["workstream_suggestion"]["workstream_id"]))


def test_other_task_invalidates_old_native_action(native):
    h, target, app, endpoint = native
    h.inbound_human("Stop this diagnostic; do not pause anything", message_id=200)
    asyncio.run(app.process_update(native_tap(app)))
    assert jobs.get_job(target)["enabled"] is True


def test_unsupported_app_restart_and_forged_payload_do_not_get_buttons(native):
    from gateway import actionable_suggestions as surface
    from gateway.platforms.base import MessageEvent
    from tests.gateway.test_trusted_human_origin_ingress import _user_source

    h, target, app, endpoint = native
    before = len([p for m, p in endpoint.calls if m == "sendMessage"])
    for action in [{"kind": "restart_app", "target": "Bambu", "token": "forged"},
                   {"kind": "pause_job", "target": target, "token": "model fabricated"}]:
        assert not asyncio.run(surface.send_offer(h.runner,
            MessageEvent(text="untrusted model/tool text", source=_user_source()), {"action": action}))
    assert len([p for m, p in endpoint.calls if m == "sendMessage"]) == before
    assert jobs.get_job(target)["enabled"] is True



def test_unsafe_target_gives_a_precise_blocker_before_execution(native):
    h, target, app, endpoint = native
    jobs.update_job(target, {"run_claim": {"owner": "synthetic active job"}})
    asyncio.run(app.process_update(native_tap(app)))
    edits = [p for m, p in endpoint.calls if m == "editMessageText"]
    assert "active run" in edits[-1]["text"]
    rows = notepad.list_receipts(scope(h), h.results[-1]["workstream_suggestion"]["workstream_id"])
    assert not any('"state":"started"' in r["detail"] for r in rows)

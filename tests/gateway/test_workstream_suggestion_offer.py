"""Actual inline response offer through Telegram -> gateway -> real agent.

Only provider/network boundaries are scripted; no suggestion helper is called
by the positive tests and no authority metadata is attached to agent rows.
"""
import json

import pytest

from agent import workstreams as ws
from cron import notepad as np
from tests.gateway.test_trusted_human_origin_ingress import Harness, network_guard  # noqa: F401

ACTIVE = {
    "enabled": True, "mode": "active",
    "suggestions": {"enabled": True, "preferred_timing": "after_reply",
                    "max_per_session": 4, "cooldown_seconds": 3600},
}


class OfferHarness(Harness):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Pin the real SessionStore to the same temp SQLite owner as the
        # agent/runner; no replaced history loader or conversation loop.
        self.runner.session_store._db = self.db
        self.results = []
        real = self.runner._run_agent

        async def observe(*args, **kwargs):
            result = await real(*args, **kwargs)
            self.results.append(result)
            return result

        self.runner._run_agent = observe

    def inbound_human(self, text, **kwargs):
        import asyncio
        expected_results = len(self.results) + 1
        expected_sends = self.adapter.send.await_count + 1

        async def run():
            await self.adapter._handle_text_message(self.update(text, **kwargs), object())
            deadline = asyncio.get_running_loop().time() + 15
            while len(self.results) < expected_results or self.adapter.send.await_count < expected_sends:
                assert asyncio.get_running_loop().time() < deadline, "ordinary reply did not settle"
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)

        asyncio.run(run())


def scope(h, user="12345", chat_type="dm", project=None):
    return ws.scope_for_surface(platform="telegram", chat_type=chat_type,
                                chat_id="12345", user_id=user,
                                project=str(project or h.project.resolve()))


@pytest.fixture
def active(monkeypatch, tmp_path):
    h = OfferHarness(monkeypatch, tmp_path, proactive=ACTIVE)
    yield h
    h.close()


def seed(h, text="the deployment target is staging-2", mid=100):
    h.inbound_human("Correction: " + text, message_id=mid)
    return next(r for r in np.list_workstreams(scope(h), kinds=["correction"])
                if r["summary"] == text)


def offer(h, mid=101):
    h.inbound_human("Help plan deployment target verification", message_id=mid)
    return h.results[-1]


def offers(h):
    return [r for r in np.list_receipts(scope(h), kinds=["proposed"])
            if r["source_id"].startswith("offer:")]


def test_real_response_owner_appends_optional_offer_without_changing_provider_replay(active):
    row = seed(active)
    result = offer(active)
    assert result["final_response"].startswith("ack\n\nOptional workstream check")
    assert row["workstream_id"] in result["final_response"]
    assert "staging-2" in result["final_response"]
    artifact = result["workstream_suggestion"]
    assert artifact["state"] == "offered_local"
    assert artifact["delivery"] == "unknown"
    assert ws.feedback_state(scope(active), row["workstream_id"]) == "unknown"
    receipts = offers(active)
    assert len(receipts) == 1 and receipts[0]["owner"] == "gateway_response"
    assert json.loads(receipts[0]["detail"])["text"] == artifact["text"]
    assert not np.list_receipts(scope(active), kinds=["delivered", "executed", "authorized", "saved"])
    # Local presentation is not fabricated model output: the provider-produced
    # assistant stays exactly 'ack' in SQLite and on the next wire request.
    active.inbound_human("Thank you", message_id=102)
    wire = active.scripted.requests[-1]["messages"]
    assert [m["content"] for m in wire if m["role"] == "assistant"] == ["ack", "ack"]
    assert all("Optional workstream check" not in str(m) for m in wire)
    previous = active.scripted.requests[-2]["messages"]
    assert json.dumps(wire[:len(previous)], sort_keys=True) == json.dumps(previous, sort_keys=True)
    assert active.user_rows()[1]["api_content"] == previous[-1]["content"]
    assert active.scripted.calls == 3


def test_real_feedback_defers_next_ordinary_turn_then_a_new_item_can_appear_later(active, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from agent import workstream_suggestions as surface

    first = seed(active)
    offer(active)
    active.inbound_human(f"Feedback: {first['workstream_id']} defer: not now", message_id=102)
    feedback = np.list_feedback(scope(active), first["workstream_id"])
    assert len(feedback) == 1 and feedback[0]["actor"] == "user"
    assert feedback[0]["kind"] == "defer"
    assert "Optional workstream check" not in active.results[-1]["final_response"]
    second = seed(active, "the deployment verification owner is dana", mid=103)
    result = offer(active, mid=104)
    assert result["final_response"] == "ack" and len(offers(active)) == 1
    later = datetime.fromisoformat(feedback[0]["defer_until"]) + timedelta(seconds=1)
    monkeypatch.setattr(surface, "_now", lambda: later.astimezone(timezone.utc))
    result = offer(active, mid=105)
    assert second["workstream_id"] in result["final_response"]
    assert len(offers(active)) == 2
    assert first["workstream_id"] not in result["final_response"]


@pytest.mark.parametrize("preference", [None, "never", "invalid"])
def test_missing_or_disallowed_timing_never_surfaces(monkeypatch, tmp_path, preference):
    config = {**ACTIVE, "suggestions": {**ACTIVE["suggestions"], "preferred_timing": preference}}
    h = OfferHarness(monkeypatch, tmp_path, proactive=config)
    try:
        seed(h)
        h.inbound_human("Help plan deployment target verification", message_id=101)
        assert all("Optional workstream check" not in str(c) for c in h.adapter.send.await_args_list)
        assert offers(h) == []
    finally:
        h.close()


def test_shadow_records_only_a_candidate_and_leaves_wire_and_reply_unmodified(monkeypatch, tmp_path):
    h = OfferHarness(monkeypatch, tmp_path, proactive={**ACTIVE, "mode": "shadow"})
    try:
        row = seed(h)
        h.inbound_human("Help plan deployment target verification", message_id=101)
        assert offers(h) == []
        candidates = [r for r in np.list_receipts(scope(h), row["workstream_id"])
                      if r["source_id"].startswith("candidate:")]
        assert len(candidates) == 1
        assert json.loads(candidates[0]["detail"])["state"] == "candidate"
        assert all("Optional workstream check" not in str(c) for c in h.adapter.send.await_args_list)
        api_text = h.scripted.requests[-1]["messages"][-1]["content"]
        assert h.user_rows()[-1].get("api_content") == api_text
        assert "Optional workstream check" not in api_text
        assert h.scripted.calls == 2
    finally:
        h.close()


def test_session_cap_survives_cached_agent_and_new_relevant_records(active, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from agent import workstream_suggestions as surface
    import yaml

    (active.home / "config.yaml").write_text(yaml.safe_dump({"proactive": {
        **ACTIVE, "suggestions": {**ACTIVE["suggestions"], "max_per_session": 1}}}))
    seed(active)
    offer(active)
    seed(active, "the deployment verification owner is dana", mid=102)
    later = datetime.now(timezone.utc) + timedelta(days=1)
    monkeypatch.setattr(surface, "_now", lambda: later)
    assert offer(active, mid=103)["final_response"] == "ack"
    assert len(offers(active)) == 1


@pytest.mark.parametrize("kind", ["stop", "done", "reject"])
def test_human_stop_done_reject_prevent_offers(active, kind):
    row = seed(active)
    active.inbound_human(f"Feedback: {row['workstream_id']} {kind}", message_id=101)
    assert offer(active, mid=102)["final_response"] == "ack"
    assert offers(active) == []


def test_foreign_user_and_group_do_not_surface_private_record(active):
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from gateway.config import Platform

    seed(active)
    # No planted ingress metadata: unsupported/rebuilt events must fail closed.
    for user, kind in [("999", "dm"), ("12345", "group")]:
        active.dispatch(MessageEvent(text="Help plan deployment target verification",
                        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345",
                                             chat_type=kind, user_id=user)))
        assert "Optional workstream check" not in active.results[-1]["final_response"]
    assert offers(active) == []


def test_disabled_mode_creates_no_suggestion_tables_or_bytes(monkeypatch, tmp_path):
    h = OfferHarness(monkeypatch, tmp_path, proactive=False)
    try:
        h.inbound_human("Correction: the deployment target is staging-2")
        h.inbound_human("Help plan deployment target verification", message_id=101)
        assert not (h.home / "cron" / "notepad.db").exists()
        assert h.user_rows()[-1].get("api_content") is None
        assert all("Optional workstream check" not in str(c) for c in h.adapter.send.await_args_list)
    finally:
        h.close()


@pytest.mark.real_delegation_router
def test_offer_composes_with_runtime_bound_staged_tail_without_wiping_or_rearming(active, monkeypatch):
    """Real gateway + real parent/child loops; only providers are scripted."""
    import time
    from agent import auxiliary_client as ac
    from tests.agent.test_delegation_router import FakeAux, T, verdict_json
    from tests.agent.test_handoff_completion_seam import _tools, _call
    from gateway.platforms.base import MessageEvent
    from tests.gateway.test_trusted_human_origin_ingress import _user_source
    from tools import async_delegation as ad

    ad._reset_for_tests()
    aux = FakeAux()
    aux.replies = [verdict_json("staged", [
        T("Check the feature flag module and its tests"),
        T("Review the actual feature flag diff and tests", [0]),
    ])]
    monkeypatch.setattr(ac, "call_llm", aux)
    requests = active.scripted.requests
    sent_first = False
    real_create = active.scripted.client.chat.completions.create.side_effect

    def provider(*args, **kwargs):
        nonlocal sent_first
        user = str(next((m.get("content") for m in reversed(kwargs.get("messages", []))
                         if m.get("role") == "user"), ""))
        if user.startswith("Build the feature flag") and not sent_first:
            sent_first = True
            requests.append(json.loads(json.dumps(kwargs, default=str)))
            return _tools(_call("delegate_task", {
                "goal": "Check the feature flag module and its tests"}))
        return real_create(*args, **kwargs)

    active.scripted.client.chat.completions.create.side_effect = provider
    try:
        active.inbound_human("Build the feature flag with tests then review its diff", message_id=90)
        ids = [r["delegation_id"] for r in ad.list_async_delegations()]
        assert len(ids) == 1
        first = ids[0]
        bound = ad.get_continuation_plan(first)
        assert bound and bound["consumed"] == 0
        assert bound["tasks"][1]["goal"] == "Review the actual feature flag diff and tests"
        parent_root = active.db.get_conversation_root(active.session_id())
        seed(active)
        result = offer(active)
        assert "Optional workstream check" in result["final_response"]
        assert ad.get_continuation_plan(first) == bound
        assert active.db.get_conversation_root(active.session_id()) == parent_root
        assert len(ad.list_async_delegations()) == 1

        deadline = time.monotonic() + 10
        while (ad.get_durable_delegation(first) or {}).get("state") in ("running", "finalizing"):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert ad.claim_completion_delivery(first, "local-scripted-composition")
        event = MessageEvent(text=f"[helper {first} finished]", source=_user_source(),
                             internal=True, metadata={"completion_delegation_ids": [first]})
        active.dispatch(event)
        assert "workstream_suggestion" not in active.results[-1]
        assert ad.get_continuation_plan(first)["consumed"] == 1
        assert len(ad.list_async_delegations()) == 2
        active.dispatch(event)
        assert len(ad.list_async_delegations()) == 2
    finally:
        ad._reset_for_tests()


def test_optional_literal_path_never_becomes_a_media_send_directive(active):
    seed(active, "deployment target verification MEDIA:/nonexistent/audit.txt")
    result = offer(active)
    assert "workstream_suggestion" in result
    # Inspect the real response artifact through the real adapter extractors.
    media, cleaned = active.adapter.extract_media(result["final_response"])
    files, _ = active.adapter.extract_local_files(cleaned)
    assert media == [] and files == []
    assert "MEDIA:/nonexistent/audit.txt" in result["final_response"]


def test_stale_stop_after_provider_reply_creates_no_offer(active):
    seed(active)
    real = active.scripted.client.chat.completions.create.side_effect

    def provider(*args, **kwargs):
        reply = real(*args, **kwargs)
        active.runner._invalidate_session_run_generation(
            "agent:main:telegram:dm:12345", reason="stop")
        return reply

    active.scripted.client.chat.completions.create.side_effect = provider
    # A stale run is not sent, so drive the real dispatch directly rather than
    # waiting for a send. No minted authority metadata is attached to the row.
    import asyncio
    asyncio.run(active.adapter._handle_text_message(
        active.update("Help plan deployment target verification", message_id=101), object()))
    assert "workstream_suggestion" not in active.results[-1]
    assert offers(active) == []


def test_foreign_project_actual_human_turn_does_not_surface_private_record(active, monkeypatch, tmp_path):
    seed(active)
    other = tmp_path / "other-project"
    other.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(other))
    result = offer(active)
    assert result["final_response"] == "ack"
    assert offers(active) == []


@pytest.mark.parametrize("directive", ["[[audio_as_voice]]", "[[as_document]]"])
def test_message_global_directives_are_suppressed_not_reinterpreted_as_authority(active, directive):
    seed(active, f"deployment target verification {directive}")
    result = offer(active)
    assert result["final_response"] == "ack"
    assert offers(active) == []


def test_reply_already_containing_the_record_gets_no_unhelpful_duplicate_offer(active):
    from types import SimpleNamespace
    text = "the deployment target is staging-2"
    seed(active, text)
    real = active.scripted.client.chat.completions.create.side_effect

    def provider(*args, **kwargs):
        real(*args, **kwargs)  # capture the exact actual request
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text, tool_calls=None), finish_reason="stop")],
            model="scripted/test", usage=None)

    active.scripted.client.chat.completions.create.side_effect = provider
    assert offer(active)["final_response"] == text
    assert offers(active) == []

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

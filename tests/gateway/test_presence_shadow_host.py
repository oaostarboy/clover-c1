"""Shadow-only presence through real Telegram/gateway/agent imports.

All emotion samples and provider replies in this file are synthetic test data.
Only network/provider edges are scripted by the existing ingress harness.
"""
import json

import yaml

from agent import workstream_suggestions as surface
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.gateway.test_workstream_suggestion_offer import ACTIVE, OfferHarness


def test_explicit_thought_reaches_shadow_seam_without_changing_reply_or_provider(monkeypatch, tmp_path):
    h = OfferHarness(monkeypatch, tmp_path, proactive=ACTIVE)
    (h.home / "config.yaml").write_text(yaml.safe_dump({
        "proactive": ACTIVE, "presence": {"enabled": True}}), encoding="utf-8")
    observed = []
    real = surface.present_offer

    def observe(agent, result, **kwargs):
        artifact = real(agent, result, **kwargs)
        observed.append(getattr(agent, "_presence_shadow", None))
        return artifact

    monkeypatch.setattr(surface, "present_offer", observe)
    try:
        h.inbound_human("Carry thought: how should we verify deployment packaging?", message_id=100)
        h.inbound_human("Help verify deployment packaging", message_id=101)
        proposal = observed[-1]
        assert proposal is not None, "explicit thought did not reach the existing shadow compose seam"
        assert proposal.action == "offer"
        assert proposal.surface is False
        assert proposal.posture == "ordinary"
        assert proposal.source_row_id == h.user_rows()[0]["id"]
        assert h.results[-1]["final_response"] == "ack"
        assert h.scripted.calls == 2
        assert all("Carry thought" not in str(m) for m in h.scripted.requests[-1]["messages"]
                   if m["role"] == "assistant")
        assert all("Optional" not in str(c) for c in getattr(h.adapter.send, "await_args_list"))
        before = h.scripted.requests[0]["messages"]
        after = h.scripted.requests[1]["messages"]
        assert json.dumps(after[:len(before)], sort_keys=True) == json.dumps(before, sort_keys=True)
    finally:
        h.close()


def test_optional_shadow_failure_does_not_remove_existing_offer(monkeypatch, tmp_path):
    from agent import presence_shadow as presence
    from tests.gateway.test_workstream_suggestion_offer import seed, offer
    h = OfferHarness(monkeypatch, tmp_path, proactive=ACTIVE)
    (h.home / "config.yaml").write_text(yaml.safe_dump({
        "proactive": ACTIVE, "presence": {"enabled": True}}), encoding="utf-8")

    def failed(*args, **kwargs):
        raise RuntimeError("synthetic presence failure")

    monkeypatch.setattr(presence, "evaluate_shadow", failed)
    try:
        seed(h)
        assert "Optional workstream check" in offer(h)["final_response"]
    finally:
        h.close()

"""Bounded action offers through the real foreground suggestion consumer."""
import pytest

from cron import jobs
from tests.gateway.test_workstream_suggestion_offer import (
    ACTIVE, OfferHarness, scope, offers, network_guard,  # noqa: F401
)


def make_diagnostic(monkeypatch, tmp_path, setup=None):
    monkeypatch.setenv("CLOVER_GATEWAY_SESSION", "1")
    h = OfferHarness(monkeypatch, tmp_path, proactive=ACTIVE)
    if setup is not None:
        setup(h)
    h.inbound_human("Hello", message_id=90)
    # Use the actual authenticated monitor-job creator, not origin metadata
    # guessed to be ownership. The inert synthetic script is never executed.
    import json
    s = scope(h)
    request = {"scope": {k: getattr(s, k) for k in ("profile", "project", "audience", "root")},
               "workstream_id": "ws_0123456789abcdef", "blocker": {
                   "title": "Offline diagnostic", "summary": "Synthetic local diagnostic"},
               "prompt": "Synthetic diagnostic only", "schedule": "every 1h",
               "monitor_script": "offline-diagnostic.sh", "approve": True}
    h.inbound_human("workstream-monitor: " + json.dumps(request), message_id=91)
    job = jobs.list_jobs()[0]
    jobs.update_job(job["id"], {"last_status": "error"})
    text = f"job {job['id']} diagnostic check repeatedly failed"
    h.inbound_human("Correction: " + text, message_id=92)
    h.inbound_human("Why did " + text + "?", message_id=93)
    return h, job["id"]


@pytest.fixture
def diagnostic(monkeypatch, tmp_path):
    h, target = make_diagnostic(monkeypatch, tmp_path)
    yield h, target
    h.close()


def test_real_suggestion_contains_a_bounded_action_not_authority(diagnostic):
    h, job_id = diagnostic
    artifact = h.results[-1]["workstream_suggestion"]
    assert artifact.get("action", {}).get("kind") == "pause_job", (
        "Relevant existing suggestions stop at optional prose; no bound action reaches approval")
    assert artifact["action"]["target"] == job_id
    assert artifact["action"]["state"] == "proposed"
    assert jobs.get_job(job_id)["enabled"] is True
    assert len(offers(h)) == 1
    assert h.scripted.calls == 4

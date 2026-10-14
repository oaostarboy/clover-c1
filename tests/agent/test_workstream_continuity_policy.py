"""Bounds, readback redaction, and canonical scope negatives (real owners)."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent import workstreams as ws
from agent.context_compressor import _audit_workstream_summary
from agent.workstream_continuity import current_turn_context, read_capsule, snapshot_for_agent
from cron import notepad
from tests.agent.test_workstream_continuity_host import (  # noqa: F401
    PATH_X, TEST_Z, _project_outcomes, compact, fresh_agent, host, produce, scope,
)
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def test_host_preflight_cannot_fall_back_to_previous_turn_scope(host, monkeypatch):
    produce(host)
    agent = fresh_agent(host)
    from agent.memory_manager import current_workstream_context

    assert "[ws:" in current_workstream_context(agent, "packaging path")
    # A cached compressor legitimately saw the prior private surface. A new
    # transport identity is presented before another turn rebuilt its scope.
    agent._user_id = "999"
    result, _, _ = compact(host, agent, monkeypatch)
    assert "[ws:" not in str(result)
    assert "Workstream evidence capsule" not in str(result)


def test_shadow_audit_never_changes_summary_from_a_previous_scope(host, monkeypatch):
    produce(host)
    shadow = replace(ws.load_settings(), mode="shadow")
    monkeypatch.setattr(ws, "load_settings", lambda: shadow)
    assert _audit_workstream_summary("byte-stable summary", scope(host)) == "byte-stable summary"
    agent = fresh_agent(host)
    assert snapshot_for_agent(agent) is None


def test_re_redaction_at_retrieval_summary_commit_and_readback(host, monkeypatch):
    _, live = produce(host)
    # Simulate pre-existing legacy owner data, not an ingress/auth receipt.
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    notepad.update_workstream(scope(host), live["workstream_id"],
                              expected_revision=live["revision"],
                              summary=f"Use {PATH_X}; {TEST_Z} failed; token {secret}")
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
    capsule = read_capsule(scope(host), ws.load_settings())
    assert capsule is not None and secret not in capsule.text
    note = current_turn_context(scope(host), "packaging path", ws.load_settings())
    assert secret not in note and PATH_X in note and TEST_Z in note
    agent = fresh_agent(host)
    result, _, calls = compact(host, agent, monkeypatch)
    assert all(secret not in str(c["messages"]) for c in calls)
    assert secret not in str(result)
    persisted = host.db.get_messages(agent.session_id)
    handoff = [r for r in persisted if "Workstream evidence capsule" in str(r["content"])][0]
    assert secret not in handoff["content"]
    assert agent._last_workstream_compaction_audit["commit_status"] == "committed"


def test_whole_literal_entries_are_bounded_and_omission_explicit(host):
    produce(host)
    settings = ws.load_settings()
    small = read_capsule(scope(host), replace(settings, threads_max_context_bytes=64))
    assert small is not None
    assert small.text == "" and len(small.omitted_ids) == 1
    normal = read_capsule(scope(host), settings)
    assert normal is not None
    assert len(normal.text.encode()) <= settings.threads_max_context_bytes
    assert PATH_X in normal.text and TEST_Z in normal.text


def test_capsule_and_readback_adapter_preserve_verified_progress_separate_from_open(host):
    produce(host)
    obligations = _project_outcomes(host)
    text = current_turn_context(scope(host), "packaging status", ws.load_settings())
    assert "obligation status=open" in text
    assert "progress status=resolved" in text
    assert obligations[0]["workstream_id"] in text
    assert "audit-check-A" in text


@pytest.mark.parametrize("foreign", ["profile", "project", "audience", "root"])
def test_no_foreign_scope_capsule_or_parent_adapter_bytes(host, foreign):
    produce(host)
    original = scope(host)
    other = replace(original, **{foreign: "foreign-scope"})
    assert read_capsule(other, ws.load_settings()) is None
    assert current_turn_context(other, "packaging path", ws.load_settings()) == ""


@pytest.mark.parametrize("kind", ["off", "shadow", "audit_off"])
def test_absent_store_remains_absent_without_audit_mutation(host, kind):
    settings = ws.load_settings()
    if kind == "off":
        settings = replace(settings, enabled=False)
    elif kind == "shadow":
        settings = replace(settings, mode="shadow")
    else:
        settings = replace(settings, compaction_audit_enabled=False)
    assert read_capsule(scope(host), settings) is None
    assert current_turn_context(scope(host), "packaging path", settings) == ""
    assert not (host.home / "cron" / "notepad.db").exists()

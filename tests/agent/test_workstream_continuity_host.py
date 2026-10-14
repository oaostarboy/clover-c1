"""Real ingress -> ledger -> native compressor -> commit -> cold readback.

Provider edges alone are scripted; the host loop, SessionDB and notepad are
real. No planted human receipt or execution authority is used.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import workstreams as ws
from agent.context_compressor import ContextCompressor
from agent.conversation_compression import CompressionCommitFence
from cron import notepad
from run_agent import AIAgent
from tests.gateway.test_trusted_human_origin_ingress import Harness, network_guard  # noqa: F401

SETTINGS = {
    "enabled": True, "mode": "active", "recall": {"enabled": True},
    "compaction_audit": {"enabled": True, "max_repairs": 0},
}
PATH_X = "/srv/app/releases/v2.3.1"
PATH_Y = "/srv/app/releases/v2.2.0"
TEST_Z = "tests/release/test_package.py::test_Z"


@pytest.fixture
def host(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=SETTINGS)
    yield h
    h.close()


def scope(h):
    return ws.scope_for_surface(platform="telegram", chat_type="dm", chat_id="12345",
                                user_id="12345", project=str(h.project.resolve()))


def produce(h):
    h.inbound_human(f"Correction: Use {PATH_Y} for packaging", message_id=100)
    old = notepad.list_workstreams(scope(h), kinds=["correction"])[0]
    h.inbound_human(
        f"Correction: {old['workstream_id']}: Use {PATH_X}, not {PATH_Y}; "
        f"{TEST_Z} failed; fix remains OPEN", message_id=101)
    h.inbound_human("Packaging investigation details: " + "reproducible local trace. " * 600,
                    message_id=102)
    h.inbound_human("Continue investigating packaging", message_id=103)
    live = [r for r in notepad.list_workstreams(scope(h), kinds=["correction"])
            if r["status"] == "open"][0]
    assert live["source"].startswith("human:")
    assert live["supersedes_id"] == old["workstream_id"]
    return old, live


def fresh_agent(h, *, in_place=True):
    agent = AIAgent(api_key="test-key", base_url="http://127.0.0.1:1/v1",
                    provider="openai", model="scripted/test", quiet_mode=True,
                    session_db=h.db, session_id=h.session_id(), platform="telegram",
                    chat_type="dm", chat_id="12345", user_id="12345",
                    skip_context_files=True, skip_memory=True)
    agent.context_compressor = ContextCompressor(
        model="scripted/test", protect_first_n=0, protect_last_n=2,
        tail_mode="legacy", quiet_mode=True)
    agent.compression_in_place = in_place
    # Feasibility probes are provider discovery, not the behavior under test.
    agent._compression_feasibility_checked = True
    return agent


def compact(h, agent, monkeypatch, *, effect=None, fence=None):
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        if effect:
            effect()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="Investigation summarized. Packaging is not verified."))])

    monkeypatch.setattr("agent.context_compressor.call_llm", provider)
    history = [{"role": r["role"], "content": r["content"]}
               for r in h.db.get_messages(agent.session_id)]
    original = json.loads(json.dumps(history))
    result, _ = agent._compress_context(history, agent._cached_system_prompt or "",
                                       force=True, commit_fence=fence or CompressionCommitFence())
    return result, original, calls


@pytest.mark.parametrize("in_place", [True, False])
def test_preflight_native_compression_audits_real_correction_before_any_turn(
        host, monkeypatch, in_place):
    old, live = produce(host)
    agent = fresh_agent(host, in_place=in_place)
    assert not getattr(agent.context_compressor, "_workstream_scope", None)
    parent = agent.session_id
    result, original, calls = compact(host, agent, monkeypatch)
    assert len(calls) == 1  # max_repairs=0 never buys another model call
    assert len(result) < len(original)
    persisted = host.db.get_messages(agent.session_id)
    text = "\n".join(str(r["content"]) for r in persisted)
    assert f"[ws:{live['workstream_id']}@r{live['revision']}]" in text
    assert PATH_X in text and PATH_Y in text and TEST_Z in text
    assert "fix remains OPEN" in text
    assert notepad.get_workstream(scope(host), old["workstream_id"])["status"] == "superseded"
    assert notepad.get_workstream(scope(host), live["workstream_id"])["status"] == "open"
    if not in_place:
        assert agent.session_id != parent
        assert host.db.get_conversation_root(agent.session_id) == parent
    audit = getattr(agent, "_last_workstream_compaction_audit", None)
    assert audit and audit["status"] == "capsule_retained"
    assert live["workstream_id"] in audit["missing_ids"]
    assert audit["repair_model_calls"] == 0
    assert audit["commit_status"] == "committed"

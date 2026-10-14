"""Real ingress -> ledger -> native compressor -> commit -> cold readback.

Provider edges alone are scripted; the host loop, SessionDB and notepad are
real. No planted human receipt or execution authority is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
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


@pytest.mark.parametrize("in_place", [True, False])
@pytest.mark.parametrize("race", ["revision", "evidence", "stop", "generation", "scope", "sqlite_failure"])
def test_real_commit_paths_refuse_stale_or_failed_evidence(host, monkeypatch, in_place, race):
    _, live = produce(host)
    agent = fresh_agent(host, in_place=in_place)
    parent = agent.session_id
    before = host.db.get_messages(parent)

    def effect():
        if race == "revision":
            notepad.update_workstream(scope(host), live["workstream_id"],
                                      expected_revision=live["revision"], status="resolved")
        elif race == "evidence":
            notepad.add_evidence(scope(host), live["workstream_id"], source_kind="project_check",
                                 source_id="late-evidence-Z", status="failed", summary="still failed")
        elif race == "stop":
            agent.hard_interrupt()
        elif race == "scope":
            agent._user_id = "foreign-user"
        elif race == "generation":
            from agent.conversation_compression import _claim_compressor_attempt
            _claim_compressor_attempt(agent.context_compressor)
        else:
            # A real SQLite trigger failure at the durable owner, not a fake DB.
            with host.db._lock:
                host.db._conn.execute("""CREATE TRIGGER reject_compaction BEFORE INSERT ON messages
                    BEGIN SELECT RAISE(ABORT, 'scripted sqlite storage failure'); END""")
                host.db._conn.commit()

    result, original, calls = compact(host, agent, monkeypatch, effect=effect)
    assert len(calls) == 1
    assert result == original
    assert agent.session_id == parent
    assert host.db.get_conversation_root(parent) == parent
    # Rotation's existing parent flush may append rows; no compacted handoff
    # and no closed parent/child publication is allowed after this failure.
    active = host.db.get_messages(parent)
    assert not any(ContextCompressor._is_context_summary_message(r) for r in active)
    assert host.db.get_session(parent)["ended_at"] is None
    assert [r["content"] for r in active[:len(before)]] == [r["content"] for r in before]
    audit = getattr(agent, "_last_workstream_compaction_audit", {})
    assert audit.get("commit_status") != "committed"


def _cold_child(root, parent, newchat):
    """A fresh interpreter, real supported gateway session reset and next turn."""
    from tests.gateway.test_trusted_human_origin_ingress import SESSION_KEY, _user_source

    monkeypatch = pytest.MonkeyPatch()
    guard = network_guard.__wrapped__(monkeypatch)
    next(guard)
    h = Harness(monkeypatch, Path(root), proactive=SETTINGS)
    try:
        entry = h.runner.session_store.get_or_create_session(_user_source())
        if newchat:
            entry = h.runner.session_store.reset_session(SESSION_KEY)
        assert entry is not None
        h.inbound_human("Which packaging path and remediation remain?", message_id=901)
        text = h.scripted.requests[-1]["messages"][-1]["content"]
        # Use the actual user API row rather than assuming the last message is
        # user for every provider format.
        text = [m["content"] for m in h.scripted.requests[-1]["messages"]
                if m["role"] == "user"][-1]
        stored = h.user_rows()[-1]
        assert stored.get("api_content") == text
        assert stored["content"] == "Which packaging path and remediation remain?"
        assert PATH_X in text and PATH_Y in text and TEST_Z in text
        assert "fix remains OPEN" in text
        assert h.db.get_conversation_root(h.session_id()) == parent
        rows = notepad.list_workstreams(scope(h))
        obligations = [r for r in rows if r["kind"] == "obligation"]
        assert obligations and all(r["status"] == "open" for r in obligations)
        assert any(r["kind"] == "progress" and r["status"] == "resolved" for r in rows)
        print("CHILD_RESULT " + json.dumps({
            "session": h.session_id(), "root": parent, "newchat": newchat,
            "open_ids": [r["workstream_id"] for r in obligations],
            "provider_calls": h.scripted.calls, "network_attempts": 0,
        }))
    finally:
        h.close()
        try:
            next(guard)
        except StopIteration:
            pass
        monkeypatch.undo()


def _project_outcomes(h):
    """Existing typed owner intake backed by real local exit codes, not prose.

    This deliberately tests persistence of the public owner contract; it does
    not claim a new automatic terminal/test outcome parser or producer.
    """
    outcomes = []
    for deliverable, code in ((TEST_Z, 1), ("audit-check-A", 0)):
        run = subprocess.run([sys.executable, "-c", f"raise SystemExit({code})"],
                             capture_output=True, check=False)
        outcomes.append(ws.TypedOutcome(
            deliverable, "failed" if run.returncode else "verified", "project_check",
            deliverable, summary=f"{deliverable} exit={run.returncode}; "
            + ("fix remains OPEN" if run.returncode else "executed verified"),
            session_id=h.session_id()))
    return ws.record_outcomes(scope(h), outcomes, ws.load_settings())


@pytest.mark.parametrize("in_place", [True, False])
@pytest.mark.parametrize("newchat", [True, False])
def test_capsule_survives_fresh_process_and_native_newchat(host, monkeypatch, in_place, newchat):
    _, correction = produce(host)
    obligations = _project_outcomes(host)
    parent = host.session_id()
    agent = fresh_agent(host, in_place=in_place)
    result, _, _ = compact(host, agent, monkeypatch)
    text = "\n".join(str(m["content"]) for m in result)
    assert "obligation status=open" in text and "progress status=resolved" in text
    assert obligations[0]["workstream_id"] in text
    assert correction["workstream_id"] in text
    if not in_place:
        from tests.gateway.test_trusted_human_origin_ingress import SESSION_KEY
        # The supported routing owner publishes the native compressor's child,
        # exactly as the outer gateway manual-compression caller does.
        advanced = host.runner.session_store.advance_compression_session(
            SESSION_KEY, parent, agent.session_id)
        assert advanced is not None and advanced.session_id == agent.session_id
    child = subprocess.run(
        [sys.executable, "-c", "from tests.agent.test_workstream_continuity_host import _cold_child; "
         "import sys; _cold_child(sys.argv[1], sys.argv[2], sys.argv[3] == 'True')",
         str(host.project.parent), parent, str(newchat)],
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stdout + child.stderr
    receipt = next(line for line in child.stdout.splitlines() if line.startswith("CHILD_RESULT "))
    observed = json.loads(receipt.removeprefix("CHILD_RESULT "))
    assert observed["root"] == parent and observed["provider_calls"] == 1
    assert observed["open_ids"] == [r["workstream_id"] for r in obligations]
    print(receipt)


@pytest.mark.parametrize("in_place", [True, False])
def test_cross_process_revision_writer_is_serialized_through_real_commit(host, monkeypatch, in_place):
    _, live = produce(host)
    agent = fresh_agent(host, in_place=in_place)
    method = "archive_and_compact" if in_place else "publish_compression_child"
    real = getattr(host.db, method)
    children = []

    def publish(*args, **kwargs):
        # The writer announces readiness before taking the existing notepad
        # BEGIN IMMEDIATE lock. It must not finish until this SessionDB commit
        # leaves the capsule guard; the two stores cannot publish stale fact
        # revisions between the audit comparison and the durable handoff.
        script = (
            "import sys; from agent import workstreams as w; from cron import notepad as n; "
            "s=w.scope_for_surface(platform='telegram',chat_type='dm',chat_id='12345',user_id='12345'); "
            "print('READY',flush=True); "
            "r=n.get_workstream(s,sys.argv[1]); "
            "n.update_workstream(s,sys.argv[1],expected_revision=r['revision'],status='resolved'); "
            "print('COMMITTED',flush=True)"
        )
        child = subprocess.Popen([sys.executable, "-c", script, live["workstream_id"]],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        children.append(child)
        assert child.stdout.readline().strip() == "READY"
        assert child.poll() is None
        return real(*args, **kwargs)

    monkeypatch.setattr(host.db, method, publish)
    result, _, _ = compact(host, agent, monkeypatch)
    assert len(children) == 1
    stdout, stderr = children[0].communicate(timeout=15)
    assert children[0].returncode == 0, stderr
    assert "COMMITTED" in stdout
    assert agent._last_workstream_compaction_audit["commit_status"] == "committed"
    assert f"[ws:{live['workstream_id']}@r{live['revision']}]" in str(result)
    assert notepad.get_workstream(scope(host), live["workstream_id"])["status"] == "resolved"


@pytest.mark.parametrize("in_place", [True, False])
def test_committed_write_with_sqlite_readback_failure_is_unknown_not_retried(host, monkeypatch, in_place):
    import sqlite3

    produce(host)
    agent = fresh_agent(host, in_place=in_place)
    method = "archive_and_compact" if in_place else "publish_compression_child"
    real_publish = getattr(host.db, method)
    real_read = host.db.get_messages
    state = {"committed": False, "writes": 0, "denials": 0}

    def publish(*args, **kwargs):
        result = real_publish(*args, **kwargs)
        state.update(committed=True, writes=state["writes"] + 1)
        return result

    def read(*args, **kwargs):
        if state["committed"] and not state["denials"]:
            def deny(action, *_args):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK
            host.db._conn.set_authorizer(deny)
            try:
                state["denials"] += 1
                return real_read(*args, **kwargs)
            finally:
                host.db._conn.set_authorizer(None)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(host.db, method, publish)
    monkeypatch.setattr(host.db, "get_messages", read)
    result, _, _ = compact(host, agent, monkeypatch)
    assert state == {"committed": True, "writes": 1, "denials": 1}
    assert agent._last_workstream_compaction_audit["commit_status"] == "unknown"
    assert "Workstream evidence capsule" in str(result)
    assert "Workstream evidence capsule" in str(real_read(agent.session_id))

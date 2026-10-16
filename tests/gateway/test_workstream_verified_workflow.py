"""Authenticated producer -> real terminal -> lifecycle verification -> draft.

Only provider/metadata/network transports are scripted. No authority rows,
HumanIngress tokens, execution IDs or verification receipts are planted.
"""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent import curator, workstreams as ws
from cron import notepad as np
from tools import write_approval as wa
from unittest.mock import MagicMock


class Script:
    def __init__(self):
        self.queue = []
        self.requests = []
        self.client = MagicMock()
        self.client.chat.completions.create.side_effect = self.create

    def create(self, **kwargs):
        self.requests.append(kwargs)
        assert self.queue, "unplanned provider call"
        return self.queue.pop(0)

    def text(self, content="Done"):
        self.queue.append(SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=None), finish_reason="stop")], usage=None))

    def terminal(self, command, cid, **kwargs):
        call = SimpleNamespace(id=cid, type="function", function=SimpleNamespace(
            name="terminal", arguments=json.dumps({"command": command, **kwargs})))
        self.queue.append(SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[call]), finish_reason="tool_calls")], usage=None))

from tests.gateway.test_trusted_human_origin_ingress import Harness, SESSION_KEY, network_guard  # noqa: F401


@pytest.fixture
def workflow(monkeypatch, tmp_path, network_guard):
    h: Any = Harness(monkeypatch, tmp_path, proactive={
        "enabled": True, "mode": "active", "procedures": {"enabled": True}})
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(np, "NOTEPAD_FILE", h.home / "cron" / "notepad.db")
    # Extend the existing hermetic fixture's ordinary agent configuration, not
    # its execution/persistence/verification path.
    import gateway.run as gr
    config = gr._load_gateway_config()
    config["agent"]["disabled_toolsets"] = []
    config["delegation"] = {"router": {"enabled": False}}
    config["terminal"] = {"env_type": "local"}
    (h.project / "manifest.txt").write_text("actual local deliverable\n")
    digest = hashlib.sha256((h.project / "manifest.txt").read_bytes()).hexdigest()
    (h.project / "manifest.sha256").write_bytes(f"{digest}  manifest.txt\n".encode("ascii"))
    script = Script()
    h.scripted.client.chat.completions.create.side_effect = script.create
    h.results = []
    h.turns = []
    h.ingresses = []
    h.config = config
    h.script = script
    from agent import workstream_procedures as producer
    real_begin = producer.begin_execution_turn

    def begin(agent, *args, **kwargs):
        token = real_begin(agent, *args, **kwargs)
        h.token = token
        return token

    monkeypatch.setattr(producer, "begin_execution_turn", begin)
    from run_agent import AIAgent
    real_run = AIAgent.run_conversation

    def observe(agent, *args, **kwargs):
        h.active_agent = agent
        h.ingresses.append(kwargs.get("trusted_human_ingress"))
        result = real_run(agent, *args, **kwargs)
        h.results.append(result)
        h.turns.append((agent, h.token, result))
        return result

    monkeypatch.setattr(AIAgent, "run_conversation", observe)
    import yaml
    (h.home / "config.yaml").write_text(yaml.safe_dump(config))
    h.n = 0

    def run(*, message="Verify SHA-256 manifest manifest.sha256",
            commands=("test -f manifest.txt", "sha256sum -c manifest.sha256"),
            new=False, on_call=None, final="Done", authenticated=True):
        if new:
            h.runner.session_store.reset_session(SESSION_KEY)
        h.n += 1
        for i, command in enumerate(commands):
            if isinstance(command, tuple):
                script.terminal(command[0], f"audit-{h.n}-{i}", **command[1])
            else:
                script.terminal(command, f"audit-{h.n}-{i}")
        script.text(final)
        before = len(h.results)
        real_create = script.create

        def create(**kwargs):
            if on_call:
                on_call(len(script.requests))
            return real_create(**kwargs)

        h.scripted.client.chat.completions.create.side_effect = create
        # Adapter + gateway mint/redeem the invocation; no test-minted token.
        async def inbound():
            update = h.update(message, message_id=500+h.n)
            if authenticated:
                await h.adapter._handle_text_message(update, SimpleNamespace())
            else:
                # Ordinary internal gateway dispatch cannot mint provenance.
                from gateway.platforms.base import MessageType
                event = h.adapter._build_message_event(update.message, MessageType.TEXT, update_id=update.update_id)
                await h.adapter.handle_message(event)
            deadline = asyncio.get_running_loop().time() + 20
            while len(h.results) == before and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.02)
            assert len(h.results) == before + 1, "real turn failed to settle"
            await asyncio.sleep(.05)
        asyncio.run(inbound())
        assert script.queue == []
        return h.results[-1]

    h.run = run
    h.scope = lambda: ws.scope_for_surface(platform="telegram", chat_type="dm", chat_id="12345", user_id="12345")
    yield h
    h.close()


def test_only_goal_verified_executions_count_toward_one_draft(workflow):
    h = workflow
    for new in (False, False, True):
        result = h.run(commands=("test -f manifest.txt", "wc -c manifest.txt"), new=new)
        assert result["workstream_execution"]["status"] == "unknown"
    assert np.list_scope_evidence(h.scope()) == []
    assert wa.list_pending("skills") == []
    roots = []
    for new in (False, False, True):
        result = h.run(new=new)
        assert result["workstream_execution"]["status"] == "success"
        roots.append(h.db.get_conversation_root(h.session_id()))
    drafts = wa.list_pending("skills")
    assert len(drafts) == 1
    assert len(set(roots)) == 2
    candidate = result["workstream_execution"]["drafts"][0]
    assert len(candidate["execution_ids"]) == 3
    content = drafts[0]["payload"]["content"]
    assert "Draft only" in content
    assert "manifest.sha256" in content
    assert all(source in content for source in candidate["execution_ids"])
    assert not list(h.home.glob("skills/**/SKILL.md"))
    assert curator.run_procedure_intake() == []
    # Actual producer IDs, original authenticated rows and acceptance receipts
    # survive in guarded stdout, not invented example data or a test ledger.
    evidence = np.list_scope_evidence(h.scope())
    origins = []
    from urllib.parse import unquote
    for run in evidence:
        _, sid, first, _, _ = run["source_id"].split(":")
        original = h.db.get_human_origin(unquote(sid), int(first))
        assert original is not None
        origins.append(vars(original))
    print("PROCEDURE_EVIDENCE " + json.dumps({"evidence": evidence, "origins": origins,
        "receipts": np.list_receipts(h.scope(), None), "draft": drafts[0]}, sort_keys=True))


@pytest.mark.parametrize("variant", [
    "no_verification", "packaging_open", "unchecked_deliverable", "failed_validation",
    "model_claim", "silent", "background", "masked_shell", "rewritten_manifest",
    "changed_artifact_after_exit_zero", "missing_artifact", "symlink", "traversal",
    "foreign_project", "foreign_profile", "foreign_audience", "current_actor", "stop",
    "stale_original", "current_root", "untrusted_source", "disabled", "shadow",
])
def test_unverified_or_wrongly_bound_outcome_never_counts(workflow, monkeypatch, variant):
    h = workflow
    commands = ("test -f manifest.txt", "sha256sum -c manifest.sha256")
    message = "Verify SHA-256 manifest manifest.sha256"
    final = "All completed, verified, saved and delivered"
    mutate = None
    if variant == "no_verification":
        commands = ("test -f manifest.txt", "wc -c manifest.txt")
    elif variant in ("packaging_open", "unchecked_deliverable"):
        message += " and package and validate the deliverable."
    elif variant == "failed_validation":
        (h.project / "manifest.txt").write_text("bad contents")
    elif variant == "model_claim":
        commands = ()
    elif variant == "silent":
        final = "[SILENT]"
    elif variant == "background":
        commands = (("test -f manifest.txt", {"background": True}), "sha256sum -c manifest.sha256")
    elif variant == "masked_shell":
        commands = ("test -f manifest.txt", "false || true")
    elif variant == "rewritten_manifest":
        mutate = lambda: (h.project / "manifest.sha256").write_text("0" * 64 + "  manifest.txt\n")
    elif variant == "changed_artifact_after_exit_zero":
        mutate = lambda: (h.project / "manifest.txt").write_text("changed after checksum tool passed")
    elif variant == "missing_artifact":
        (h.project / "manifest.txt").unlink()
    elif variant == "symlink":
        original = h.project / "original.txt"
        (h.project / "manifest.txt").rename(original)
        (h.project / "manifest.txt").symlink_to(original)
    elif variant == "traversal":
        source = h.project.parent / "outside.txt"
        source.write_text("outside")
        (h.project / "manifest.sha256").write_text(hashlib.sha256(source.read_bytes()).hexdigest() + "  ../outside.txt\n")
    elif variant == "foreign_project":
        other = h.project.parent / "other-project"; other.mkdir()
        mutate = lambda: monkeypatch.setenv("TERMINAL_CWD", str(other))
    elif variant == "foreign_profile":
        other = h.home.parent / "other-profile"; other.mkdir()
        mutate = lambda: monkeypatch.setenv("CLOVER_HOME", str(other))
    elif variant == "foreign_audience":
        mutate = lambda: setattr(h.active_agent, "_chat_type", "group")
    elif variant == "current_actor":
        mutate = lambda: setattr(h.active_agent, "_user_id", "other")
    elif variant == "stop":
        # The actual gateway's run-generation fence, not forged provenance.
        mutate = lambda: h.runner._invalidate_session_run_generation(SESSION_KEY, reason="test Stop")
    elif variant in ("stale_original", "current_root"):
        h.run()
        old = h.ingresses[-1]
        assert old is not None
        mutate = lambda: setattr(h.active_agent, "_trusted_human_ingress", old)
    elif variant in ("disabled", "shadow"):
        import yaml
        h.config["proactive"]["mode"] = "shadow"
        if variant == "disabled":
            h.config["proactive"]["enabled"] = False
        (h.home / "config.yaml").write_text(yaml.safe_dump(h.config))
    before = len(np.list_scope_evidence(h.scope())) if variant not in ("disabled", "shadow") else 0
    at = len(h.script.requests) + len(commands)
    def callback(n):
        if mutate and n == at:
            mutate()
    result = h.run(commands=commands, message=message, final=final, on_call=callback,
                   new=variant == "current_root", authenticated=variant != "untrusted_source")
    assert result.get("workstream_execution", {}).get("status", "unknown") != "success"
    # Restore only test environment for scoped readback, never live settings.
    monkeypatch.setenv("CLOVER_HOME", str(h.home))
    monkeypatch.setenv("TERMINAL_CWD", str(h.project))
    assert len(np.list_scope_evidence(h.scope())) == before
    assert wa.list_pending("skills") == []


def test_duplicate_execution_after_retirement_cannot_increment(workflow):
    from agent.workstream_procedures import finish_execution_turn
    h = workflow
    h.run()
    agent, turn, result = h.turns[-1]
    before = np.list_scope_evidence(h.scope())
    assert len(before) == 1
    replay = finish_execution_turn(agent, result, turn)
    assert replay["status"] == "unknown"
    assert np.list_scope_evidence(h.scope()) == before
    assert wa.list_pending("skills") == []


def test_three_runs_in_one_authentic_root_are_not_enough(workflow):
    for _ in range(3):
        assert workflow.run()["workstream_execution"]["status"] == "success"
    assert len(np.list_scope_evidence(workflow.scope())) == 3
    assert wa.list_pending("skills") == []


def test_original_receipt_change_during_acceptance_rolls_back_atomically(workflow, monkeypatch):
    h = workflow
    real_add = np.add_receipt
    def change_original(*args, **kwargs):
        saved = real_add(*args, **kwargs)
        if kwargs.get("kind") == "executed":
            agent = h.active_agent
            original = next(r for r in agent._session_db.get_messages(agent.session_id)
                            if r["role"] == "user")
            # Existing SessionDB consumer API, not planted authority rows.
            assert agent._session_db.consume_human_origin(
                agent.session_id, original["id"], consumer="test_concurrent_consumer")
        return saved
    monkeypatch.setattr(np, "add_receipt", change_original)
    result = h.run()
    assert result["workstream_execution"]["status"] == "unknown"
    assert np.list_scope_evidence(h.scope()) == []
    assert np.list_receipts(h.scope(), None, kinds=["executed"]) == []
    assert wa.list_pending("skills") == []

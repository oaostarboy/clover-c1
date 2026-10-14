"""Real ordinary loop -> terminal owner -> SQLite -> curator -> pending DRAFT.

No host/executor replacements. Only provider/metadata/auxiliary network edges
are scripted; socket and DNS attempts fail. All effects stay in temp HOME.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from agent import curator, workstreams as ws
from clover_state import SessionDB
from cron import notepad as np
from run_agent import AIAgent
from tools import write_approval as wa
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


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

    def text(self, content="Done; all saved and delivered"):
        self.queue.append(SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None),
                                     finish_reason="stop")], usage=None))

    def terminal(self, command, cid, **kwargs):
        call = SimpleNamespace(id=cid, type="function", function=SimpleNamespace(
            name="terminal", arguments=json.dumps({"command": command, **kwargs})))
        self.queue.append(SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]),
                                     finish_reason="tool_calls")], usage=None))


@pytest.fixture
def host(monkeypatch, tmp_path, network_guard):
    home = tmp_path / "profile"
    home.mkdir()
    os_home = tmp_path / "home"
    os_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "manifest.txt").write_text("real local audit artifact\n")
    digest = hashlib.sha256((project / "manifest.txt").read_bytes()).hexdigest()
    (project / "manifest.sha256").write_text(f"{digest}  manifest.txt\n")
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(np, "NOTEPAD_FILE", home / "cron" / "notepad.db")
    config = {"proactive": {"enabled": True, "mode": "active", "procedures": {"enabled": True}},
              "compression": {"enabled": False}, "delegation": {"router": {"enabled": False}},
              "terminal": {"env_type": "local"}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    script = Script()
    monkeypatch.setattr(AIAgent, "_create_openai_client", lambda *a, **k: script.client)
    for target in ("agent.model_metadata.get_model_context_length",
                   "agent.context_compressor.get_model_context_length"):
        monkeypatch.setattr(target, lambda *a, **k: 100_000)
    for target in ("agent.model_metadata.query_ollama_num_ctx", "agent.agent_init.query_ollama_num_ctx"):
        monkeypatch.setattr(target, lambda *a, **k: None)
    import agent.auxiliary_client as aux

    def forbidden(*a, **k):
        raise AssertionError("unexpected auxiliary provider call")

    monkeypatch.setattr(aux, "call_llm", forbidden)
    monkeypatch.setattr(aux, "async_call_llm", forbidden)
    db = SessionDB(home / "state.db")
    agents = {}
    turns = []

    def run(sid, *, commands=("test -f manifest.txt", "sha256sum -c manifest.sha256"), history=None):
        if sid not in agents:
            agents[sid] = AIAgent(provider="openai-compat", model="scripted-offline",
                base_url="http://127.0.0.1:1/v1",
                api_mode="chat_completions", api_key="test-key", model_pinned=True,
                enabled_toolsets=["terminal"], max_iterations=8, quiet_mode=True,
                tool_progress_mode="off", skip_context_files=True, load_soul_identity=False,
                skip_memory=True, skip_background_review=True, platform="cli",
                session_id=sid, session_db=db)
        agent = agents[sid]
        from agent.workstream_procedures import begin_execution_turn
        token = begin_execution_turn(agent, history or [])
        n = len(script.requests)
        for i, command in enumerate(commands):
            script.terminal(command, f"call-{sid}-{n}-{i}")
        script.text()
        result = agent.run_conversation("Verify the local manifest", conversation_history=history or [],
                                        task_id=f"audit-{sid}-{n}")
        assert not script.queue
        turns.append((agent, token, result))
        return result

    h = SimpleNamespace(home=home, project=project, config=config, db=db, script=script,
                        agents=agents, turns=turns, run=run)
    h.scope = lambda: ws.scope_for_agent(next(iter(agents.values())))
    yield h
    db.close()


def test_three_real_verified_workflows_two_sessions_stage_one_attributed_draft(host):
    first = host.run("authentic-a")
    assert first["workstream_execution"]["status"] == "success"
    assert curator.run_procedure_intake() == []
    host.run("authentic-a")
    assert curator.run_procedure_intake() == []
    third = host.run("authentic-b")
    candidates = third["workstream_execution"]["drafts"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert len(candidate["execution_ids"]) == 3
    assert candidate["session_ids"] == ["authentic-a", "authentic-b"]
    drafts = wa.list_pending("skills")
    assert len(drafts) == 1
    assert drafts[0]["action"] == "create"
    assert drafts[0]["origin"] == "background_review"
    assert set(drafts[0]["payload"]) == {"action", "name", "content"}
    content = drafts[0]["payload"]["content"]
    from tools.skill_manager_tool import _validate_frontmatter, _validate_name
    assert _validate_name(drafts[0]["payload"]["name"]) is None
    assert _validate_frontmatter(content, new_skill=True) is None
    assert "sha256sum -c manifest.sha256" in content
    for source in candidate["execution_ids"]:
        assert source in content
    assert "Draft only" in content
    assert not list(host.home.glob("skills/**/SKILL.md"))
    receipts = np.list_receipts(host.scope(), None)
    assert {r["kind"] for r in receipts} == {"executed", "proposed"}
    assert len([r for r in receipts if r["kind"] == "executed"]) == 3
    assert curator.run_procedure_intake() == []
    assert len(wa.list_pending("skills")) == 1
    assert wa.discard_pending("skills", drafts[0]["id"])
    assert curator.run_procedure_intake() == []
    assert wa.list_pending("skills") == []


@pytest.mark.parametrize("commands,status", [((), "unknown"),
    (("false", "sha256sum -c manifest.sha256"), "failed"),
    (("test -f manifest.txt",), "unknown"), (("true", "true"), "unknown"),
    (("test -f manifest.txt", "false || true"), "unknown")])
def test_prose_failed_unknown_and_transfer_missing_do_not_create_candidates(host, commands, status):
    for sid in ("a", "a", "b"):
        result = host.run(sid, commands=commands)
        assert result["workstream_execution"]["status"] == status
    assert curator.run_procedure_intake() == []
    assert np.list_scope_evidence(host.scope()) == []
    assert wa.list_pending("skills") == []


def test_exact_same_owner_receipt_replay_does_not_increment(host):
    from agent.workstream_procedures import finish_execution_turn

    host.run("replay-source")
    agent, token, result = host.turns[-1]
    scope = host.scope()
    before = np.list_scope_evidence(scope)
    replay = finish_execution_turn(agent, result, token)
    after = np.list_scope_evidence(scope)
    assert replay is not None
    assert replay["status"] == "success"
    assert replay["execution_ids"] == [before[0]["source_id"]]
    assert [r["evidence_id"] for r in after] == [r["evidence_id"] for r in before]
    assert len(np.list_receipts(scope, None, kinds=["executed"])) == 1


def test_two_curator_consumers_racing_stage_exactly_one_draft(host, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    real_intake = curator.run_procedure_intake
    # Delay only the consumer; all three producers still run through the real
    # host loop, terminal owner, persistence and SQLite evidence path.
    monkeypatch.setattr(curator, "run_procedure_intake", lambda **_kw: [])
    host.run("race-a")
    host.run("race-a")
    host.run("race-b")
    monkeypatch.setattr(curator, "run_procedure_intake", real_intake)
    original = host.scope()
    foreign = ws.Scope(profile=original.profile, project=str(host.project / "foreign"),
                       audience=original.audience, root=original.root)
    foreign_wid = np.create_workstream(
        foreign, kind="progress", title="copied", summary="copied",
        fingerprint="copied-execution-evidence",
    )["workstream_id"]
    for evidence in np.list_scope_evidence(original):
        np.add_receipt(foreign, foreign_wid, kind="executed", owner="execution",
                       source_id=evidence["source_id"])
        np.add_evidence(
            foreign, foreign_wid, source_kind="execution",
            source_id=evidence["source_id"], status="verified",
            trust="owner_session", key="procedure_run",
            procedure_key=evidence["procedure_key"], session_id=evidence["session_id"],
            summary=evidence["summary"],
        )
    assert curator.propose_procedure_candidates(foreign, ws.load_settings()) == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: real_intake(), range(2)))
    assert sum(len(rows) for rows in results) == 1
    assert len(wa.list_pending("skills")) == 1
    proposal_occurrences = [
        row for row in np.list_occurrences(host.scope())
        if row["occurrence_key"].startswith("procedure-proposal:")
    ]
    assert len(proposal_occurrences) == 1
    assert proposal_occurrences[0]["state"] == "executed"


def test_disabled_and_shadow_change_no_feature_files_or_result(host):
    for section in ({"enabled": False}, {"enabled": True, "mode": "shadow", "procedures": {"enabled": True}}):
        host.config["proactive"] = section
        (host.home / "config.yaml").write_text(yaml.safe_dump(host.config))
        before = {str(p.relative_to(host.home)): p.read_bytes() for p in host.home.rglob("*")
                  if p.is_file() and ("skills" in p.parts or "cron" in p.parts)}
        result = host.run("off")
        assert "workstream_execution" not in result
        assert curator.run_procedure_intake() == []
        after = {str(p.relative_to(host.home)): p.read_bytes() for p in host.home.rglob("*")
                 if p.is_file() and ("skills" in p.parts or "cron" in p.parts)}
        assert before == after


def test_legacy_consumed_transport_completion_cannot_become_a_procedure(host):
    import time
    import tools.async_delegation as ad
    host.run("real", commands=())
    scope = host.scope()
    wid = np.create_workstream(scope, kind="progress", title="old transport", summary="old",
                               fingerprint="old")["workstream_id"]
    for i in range(3):
        did = f"legacy-{i}"
        ad._persist_dispatch({"delegation_id": did, "dispatched_at": time.time(), "goal": "audit"})
        ad._persist_completion({"delegation_id": did, "status": "completed"}, {"summary": "success"})
        ad.mark_completion_delivered(did)
        np.add_receipt(scope, wid, kind="executed", owner="execution", source_id=did)
        np.add_evidence(scope, wid, source_kind="delegation", source_id=did, status="verified",
                        key="procedure_run", procedure_key="legacy", session_id=f"invented-{i}",
                        summary="run audit", trust="owner_session")
    assert curator.run_procedure_intake() == []
    assert wa.list_pending("skills") == []

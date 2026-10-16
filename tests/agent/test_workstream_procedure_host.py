"""Real ordinary loop -> terminal owner -> SQLite -> curator -> pending DRAFT.

No host/executor replacements. Only provider/metadata/auxiliary network edges
are scripted; socket and DNS attempts fail. All effects stay in temp HOME.
"""
import pytest
import yaml

from agent import curator, workstreams as ws
from cron import notepad as np
from tools import write_approval as wa
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.gateway.test_workstream_verified_workflow import workflow  # noqa: F401


@pytest.fixture
def host(workflow):
    """Legacy test IDs now use real authenticated ingress, not CLI roles.

    The gateway/adapter produce all sessions, original-human receipts and turn
    tokens. The aliases below select actual conversation resets only.
    """
    h = workflow
    actual_run = h.run
    h.agents = {}
    h.last_alias = None

    def run(sid, *, commands=("test -f manifest.txt", "sha256sum -c manifest.sha256"), history=None):
        assert not history, "imported history is not an originating human request"
        new = h.last_alias is not None and h.last_alias != sid
        result = actual_run(commands=commands, new=new)
        h.last_alias = sid
        h.agents[sid] = h.active_agent
        return result

    h.run = run
    yield h


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
    assert len(candidate["session_ids"]) == 2
    assert set(candidate["session_ids"]) == {
        host.db.get_conversation_root(a.session_id) for a in host.agents.values()}
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
    assert replay["status"] == "unknown"  # original live invocation retired
    assert replay["execution_ids"] == []
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

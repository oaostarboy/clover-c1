"""Authenticated host -> real pytest -> exact accepted work -> durable capsule."""
import hashlib
import json

from agent import workstreams as ws
from cron import notepad as np
from tests.gateway.test_workstream_verified_workflow import workflow, network_guard  # noqa: F401
import pytest


@pytest.fixture(autouse=True)
def isolated_terminal_owner():
    # Each gateway fixture owns a new project. The real local terminal owner
    # otherwise retains task 'default' and its cwd across test cases.
    from tools.terminal_tool import cleanup_all_environments
    cleanup_all_environments()
    yield
    cleanup_all_environments()


def contract(h, *, covers=None, rows=None):
    # These are accepted tasks, not planted verification or provenance receipts.
    scope = h.scope()
    sid = h.session_id()
    if rows is None:
        rows = ws.record_outcomes(scope, [
            ws.TypedOutcome("Build and test candidate", "failed", "delegation", "original", session_id=sid),
            ws.TypedOutcome("Review and release candidate", "not_run", "rollout", "release", session_id=sid),
        ], ws.load_settings())
    probe = h.project / "acceptance_probe.py"
    probe.write_text("def test_candidate():\n    assert 2 + 2 == 4\n")
    command = "python -m pytest acceptance_probe.py --junitxml=acceptance.xml -q"
    spec = {
        "version": 1,
        "coverage": [{"workstream_id": rows[0]["workstream_id"], "revision": rows[0]["revision"],
                      "session_id": sid, "checks": [0]}] if covers is None else covers,
        "artifacts": {"acceptance_probe.py": hashlib.sha256(probe.read_bytes()).hexdigest()},
        "checks": [{"command": command, "report": "acceptance.xml", "min_tests": 1}],
    }
    (h.project / "acceptance.json").write_text(json.dumps(spec))
    return rows, command, spec


def test_real_owner_checks_close_only_exact_covered_work(workflow):
    h = workflow
    # Establish a real authenticated session before accepting work in it.
    h.run(commands=(), final="Ready")
    rows, command, _ = contract(h)
    result = h.run(message="Verify work acceptance acceptance.json", commands=((command, {"workdir": str(h.project)}),))
    current = np.get_workstream(h.scope(), rows[0]["workstream_id"])
    assert current["status"] == "resolved"
    assert "verified" in current["summary"] and "unverified" not in current["summary"]
    assert np.get_workstream(h.scope(), rows[1]["workstream_id"]) == rows[1]
    evidence = np.list_evidence(h.scope(), current["workstream_id"])
    assert any(e["source_id"] == "original" and e["status"] == "failed" for e in evidence)
    accepted = [e for e in evidence if e["source_kind"] == "acceptance" and e["status"] == "verified"]
    assert len(accepted) == 1
    assert accepted[0]["trust"] == "owner_session"
    receipts = np.list_receipts(h.scope(), current["workstream_id"])
    assert len(receipts) == 1
    proof = json.loads(receipts[0]["detail"])
    assert proof["check_ids"] and proof["artifact_digest"] and proof["spec_digest"]
    contracts = [r for r in np.list_history(h.scope(), current["workstream_id"])
                 if r["op"] == "acceptance_contract"]
    assert len(contracts) == 1, "the owner must retain the pre-execution contract for later audit"
    saved_contract = json.loads(contracts[0]["detail"])
    assert hashlib.sha256(saved_contract["contract"].encode()).hexdigest() == proof["spec_digest"]
    assert json.loads(saved_contract["contract"])["coverage"][0]["workstream_id"] == current["workstream_id"]
    assert saved_contract["proof"] == proof
    assert result["workstream_execution"]["status"] == "success"
    # A fresh read, not instance lineage, removes the completed warning and
    # keeps the separate review/install/release work visible.
    text = ws.thread_context(h.scope(), ws.load_settings())
    assert "Build and test candidate" not in text
    assert "Review and release candidate" in text


def test_changed_report_during_commit_cannot_verify(workflow, monkeypatch):
    h = workflow
    h.run(commands=(), final="Ready")
    rows, command, _ = contract(h)
    real_add = np.add_receipt
    mutated = []

    def replace_report(*args, **kwargs):
        saved = real_add(*args, **kwargs)
        if kwargs.get("owner") == "acceptance":
            mutated.append(True)
            (h.project / "acceptance.xml").write_text("<testsuite tests='0'/>")
        return saved

    monkeypatch.setattr(np, "add_receipt", replace_report)
    result = h.run(message="Verify work acceptance acceptance.json", commands=((command, {"workdir": str(h.project)}),))
    assert mutated, {"token": h.token, "tools": [m.get("content") for m in result["messages"] if m.get("role") == "tool"]}
    assert np.get_workstream(h.scope(), rows[0]["workstream_id"])["status"] == "open"
    assert not np.list_receipts(h.scope(), rows[0]["workstream_id"])
    assert result["workstream_execution"]["status"] != "success"


@pytest.mark.parametrize("variant", [
    "helper_prose", "untrusted", "zero_only", "stale_report", "failed_test", "skipped_test",
    "empty_report", "bad_report", "report_totals", "changed_artifact", "changed_spec", "changed_evidence",
    "changed_revision", "wrong_session", "new_session", "foreign_id", "foreign_scope",
    "stop", "stop_during_commit", "origin_consumed_during_commit", "silent", "shadow",
    "disabled", "expired", "duplicate_coverage", "partial_check_coverage", "wrong_cwd",
])
def test_acceptance_fails_closed(workflow, monkeypatch, variant):
    h = workflow
    h.run(commands=(), final="Ready")
    rows, command, spec = contract(h)
    scope = h.scope()
    commands = ((command, {"workdir": str(h.project)}),)
    message = "Verify work acceptance acceptance.json"
    mutate = None
    if variant == "helper_prose":
        commands = ()
    elif variant == "zero_only":
        spec["checks"][0]["command"] = "true"
        commands = (("true", {"workdir": str(h.project)}),)
    elif variant == "stale_report":
        (h.project / "acceptance.xml").write_text("<testsuite tests='1'><testcase name='old'/></testsuite>")
    elif variant in ("failed_test", "skipped_test"):
        probe = h.project / "acceptance_probe.py"
        probe.write_text("def test_candidate():\n    assert False\n" if variant == "failed_test"
                         else "import pytest\ndef test_candidate():\n    pytest.skip('not verified')\n")
        spec["artifacts"]["acceptance_probe.py"] = hashlib.sha256(probe.read_bytes()).hexdigest()
    elif variant in ("empty_report", "bad_report"):
        text = "<testsuite tests='0'/>" if variant == "empty_report" else "not XML"
        mutate = lambda: (h.project / "acceptance.xml").write_text(text)
    elif variant == "report_totals":
        mutate = lambda: (h.project / "acceptance.xml").write_text(
            "<testsuite tests='0' failures='1' errors='0' skipped='0'><testcase name='fake'/></testsuite>")
    elif variant == "changed_artifact":
        mutate = lambda: (h.project / "acceptance_probe.py").write_text("changed after tests passed")
    elif variant == "changed_spec":
        mutate = lambda: (h.project / "acceptance.json").write_text("{}")
    elif variant == "changed_evidence":
        mutate = lambda: np.add_evidence(scope, rows[0]["workstream_id"], source_kind="project_check",
            source_id="concurrent-failure", status="failed", summary="new failure")
    elif variant == "changed_revision":
        mutate = lambda: np.update_workstream(scope, rows[0]["workstream_id"],
            expected_revision=rows[0]["revision"], summary="owner changed acceptance")
    elif variant == "wrong_session":
        spec["coverage"][0]["session_id"] = "other-session"
    elif variant == "foreign_id":
        spec["coverage"][0]["workstream_id"] = "ws_0000000000000000"
    elif variant == "foreign_scope":
        mutate = lambda: setattr(h.active_agent, "_chat_type", "group")
    elif variant in ("stop", "stop_during_commit"):
        from tests.gateway.test_workstream_verified_workflow import SESSION_KEY
        mutate = lambda: h.runner._invalidate_session_run_generation(SESSION_KEY, reason="test Stop")
    elif variant == "expired":
        from dataclasses import replace
        def expire():
            object.__setattr__(h.token, "acceptance", replace(h.token.acceptance, started=h.token.acceptance.started - 301))
        mutate = expire
    elif variant in ("disabled", "shadow"):
        h.config["proactive"]["mode"] = "shadow"
        if variant == "disabled":
            h.config["proactive"]["enabled"] = False
        import yaml
        (h.home / "config.yaml").write_text(yaml.safe_dump(h.config))
    elif variant == "duplicate_coverage":
        spec["coverage"].append(dict(spec["coverage"][0]))
    elif variant == "partial_check_coverage":
        spec["checks"].append({"command": "python -m pytest second.py --junitxml=second.xml -q",
                               "report": "second.xml", "min_tests": 1})
        spec["coverage"][0]["checks"].append(1)
    elif variant == "wrong_cwd":
        commands = ((command, {}),)
    (h.project / "acceptance.json").write_text(json.dumps(spec))
    if variant in ("stop_during_commit", "origin_consumed_during_commit"):
        real_add = np.add_receipt
        def race(*args, **kwargs):
            receipt = real_add(*args, **kwargs)
            if kwargs.get("owner") == "acceptance":
                if variant == "stop_during_commit":
                    mutate()
                else:
                    agent = h.active_agent
                    row = next(r for r in reversed(agent._session_db.get_messages(agent.session_id, latest=True, limit=20))
                               if r["role"] == "user")
                    assert agent._session_db.consume_human_origin(agent.session_id, row["id"], consumer="race")
            return receipt
        monkeypatch.setattr(np, "add_receipt", race)
    at = len(h.script.requests) + len(commands)
    def callback(n):
        if mutate and n == at and variant != "stop_during_commit":
            mutate()
    result = h.run(message=message, commands=commands, on_call=callback,
                   authenticated=variant != "untrusted", new=variant == "new_session",
                   final="[SILENT]" if variant == "silent" else "All done and verified")
    current = np.get_workstream(scope, rows[0]["workstream_id"])
    assert current["status"] == "open"
    assert not any(e["status"] == "verified" for e in np.list_evidence(scope, rows[0]["workstream_id"]))
    assert np.list_receipts(scope, rows[0]["workstream_id"]) == []
    assert np.get_workstream(scope, rows[1]["workstream_id"]) == rows[1]
    assert result.get("workstream_execution", {}).get("status", "unknown") != "success"


def test_concurrent_owner_acceptance_commits_once(workflow, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from agent import workstream_acceptance as acceptance
    h = workflow
    h.run(commands=(), final="Ready")
    rows, command, _ = contract(h)
    real_finish = acceptance.finish
    results = []

    def concurrent(*args):
        def attempt():
            try:
                return real_finish(*args)
            except (ValueError, np.StaleRevision):
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results.extend(pool.map(lambda _: attempt(), range(2)))
        return next(r for r in results if r is not None)

    monkeypatch.setattr(acceptance, "finish", concurrent)
    h.run(message="Verify work acceptance acceptance.json",
          commands=((command, {"workdir": str(h.project)}),))
    assert sum(r is not None for r in results) == 1
    assert np.get_workstream(h.scope(), rows[0]["workstream_id"])["status"] == "resolved"
    assert len(np.list_receipts(h.scope(), rows[0]["workstream_id"])) == 1
    assert len([e for e in np.list_evidence(h.scope(), rows[0]["workstream_id"])
                if e["source_kind"] == "acceptance"]) == 1


def test_real_lifecycle_handback_accepts_explicit_ancestor_coverage_only(workflow):
    from types import SimpleNamespace
    from agent import work_lifecycle as wl
    from tests.agent.test_workstream_lineage import handback, complete
    h = workflow
    h.run(commands=(), final="Ready")
    lifecycle = SimpleNamespace(agent=h.active_agent, scope=h.scope(), settings=ws.load_settings())
    original = handback(lifecycle, first_result={"incomplete": True})
    complete("deleg_126703c7", summary="Everything fixed, reviewed and released")
    wl.begin_turn(h.active_agent, "", "internal", ["deleg_126703c7"], may_note=False)
    wl.end_turn(h.active_agent)
    rows = np.list_workstreams(h.scope(), kinds=["obligation"])
    assert len(rows) == 2 and all(r["status"] == "open" for r in rows)
    covers = [{"workstream_id": r["workstream_id"], "revision": r["revision"],
               "session_id": h.session_id(), "checks": [0]} for r in rows]
    _, command, _ = contract(h, covers=covers, rows=rows)
    result = h.run(message="Verify work acceptance acceptance.json",
                   commands=((command, {"workdir": str(h.project)}),))
    assert result["workstream_execution"]["status"] == "success"
    assert all(np.get_workstream(h.scope(), r["workstream_id"])["status"] == "resolved" for r in rows)
    assert any(e["status"] == "failed" for e in np.list_evidence(h.scope(), original["workstream_id"]))
    assert "Unfinished:" not in ws.thread_context(h.scope(), ws.load_settings())
    # Replaying the old helper result cannot reopen accepted work or add notes.
    before = np.list_workstreams(h.scope())
    wl.begin_turn(lifecycle.agent, "", "internal", ["deleg_126703c7"], may_note=False)
    wl.end_turn(lifecycle.agent)
    assert np.list_workstreams(h.scope()) == before


def test_acceptance_does_not_require_procedure_learning_or_duplicate_replay(workflow):
    h = workflow
    h.run(commands=(), final="Ready")
    rows, command, _ = contract(h)
    import yaml
    h.config["proactive"]["procedures"]["enabled"] = False
    (h.home / "config.yaml").write_text(yaml.safe_dump(h.config))
    result = h.run(message="Verify work acceptance acceptance.json",
                   commands=((command, {"workdir": str(h.project)}),))
    assert np.get_workstream(h.scope(), rows[0]["workstream_id"])["status"] == "resolved"
    before = (np.list_workstreams(h.scope()), np.list_scope_evidence(h.scope()), np.list_receipts(h.scope()))
    from agent.workstream_procedures import finish_execution_turn
    agent, turn, _ = h.turns[-1]
    assert finish_execution_turn(agent, result, turn)["status"] == "unknown"
    assert (np.list_workstreams(h.scope()), np.list_scope_evidence(h.scope()), np.list_receipts(h.scope())) == before

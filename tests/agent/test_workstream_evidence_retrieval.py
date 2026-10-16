"""Bounded, profile-scoped retrieval from existing evidence owners."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def _agent(scope, *, enabled=True, recall=True):
    return SimpleNamespace(
        _workstream_scope=scope,
        _workstream_settings={
            "proactive": {
                "enabled": enabled,
                "recall": {"enabled": recall, "max_bytes": 1200},
            }
        },
    )


def _scope(project, profile):
    from agent.workstreams import make_scope

    return make_scope(str(project), profile=str(profile), root="local")


def _add_session(db, *, session_id, project, content, timestamp=None):
    db.create_session(session_id, "cli", cwd=str(project))
    return db.append_message(
        session_id, "user", content,
        timestamp=timestamp or datetime(2026, 10, 14, tzinfo=timezone.utc),
    )


def test_public_result_is_immutable_and_empty_query_is_explicit_unknown(tmp_path):
    import clover_constants
    from agent.workstream_evidence import TurnEvidence, retrieve_turn_evidence
    from agent.workstreams import current_profile

    profile = tmp_path / "profile"
    profile.mkdir()
    token = clover_constants.set_clover_home_override(profile)
    try:
        scope = _scope(tmp_path, current_profile())
        result = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="", max_bytes=1200,
            now=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
    finally:
        clover_constants.reset_clover_home_override(token)

    assert isinstance(result, TurnEvidence)
    assert result.status == "unknown"
    assert result.text
    assert result.source_ids == ()
    assert result.missing_keys == ("evidence",)
    with pytest.raises((AttributeError, TypeError)):
        result.status = "complete"


def test_disabled_and_mismatched_scope_return_empty_text(tmp_path):
    import clover_constants
    from agent.workstream_evidence import retrieve_turn_evidence
    from agent.workstreams import current_profile, make_scope

    profile = tmp_path / "profile"
    profile.mkdir()
    token = clover_constants.set_clover_home_override(profile)
    try:
        scope = _scope(tmp_path, current_profile())
        disabled = retrieve_turn_evidence(
            _agent(scope, enabled=False), scope=scope, query="tests passed"
        )
        other = make_scope(str(tmp_path / "other"), profile=current_profile(), root="local")
        unmatched = retrieve_turn_evidence(
            _agent(scope), scope=other, query="tests passed"
        )
    finally:
        clover_constants.reset_clover_home_override(token)

    assert disabled.status == "disabled"
    assert disabled.text == ""
    assert disabled.source_ids == ()
    assert unmatched.status == "unknown"
    assert unmatched.text == ""
    assert unmatched.source_ids == ()


def test_session_evidence_uses_profile_sqlite_and_exact_message_citation(tmp_path):
    import clover_constants
    from agent.workstream_evidence import retrieve_turn_evidence
    from agent.workstreams import current_profile
    from clover_state import SessionDB

    profile, project = tmp_path / "profile-a", tmp_path / "project"
    profile.mkdir()
    project.mkdir()
    token = clover_constants.set_clover_home_override(profile)
    db = SessionDB(db_path=profile / "state.db")
    try:
        message_id = _add_session(
            db, session_id="session-a", project=project,
            content="The deployment tests passed in this project.",
        )
        scope = _scope(project, current_profile())
        result = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="deployment tests passed",
            now=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
    finally:
        db.close()
        clover_constants.reset_clover_home_override(token)

    citation = f"session:session-a:message:{message_id}"
    assert result.status == "partial"
    assert result.source_ids == (citation,)
    assert citation in result.text
    assert "deployment tests passed" in result.text
    assert "Unverified user transcript statement" in result.text
    assert "verified_outcome" in result.missing_keys


def test_profile_and_project_isolation_rejects_foreign_history(tmp_path):
    import clover_constants
    from agent.workstream_evidence import retrieve_turn_evidence
    from agent.workstreams import current_profile, make_scope
    from clover_state import SessionDB

    project = tmp_path / "project"
    project.mkdir()
    profile_a, profile_b = tmp_path / "profile-a", tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    token = clover_constants.set_clover_home_override(profile_a)
    db = SessionDB(db_path=profile_a / "state.db")
    try:
        _add_session(db, session_id="private-a", project=project,
                     content="The private migration verification passed.")
    finally:
        db.close()
        clover_constants.reset_clover_home_override(token)

    token = clover_constants.set_clover_home_override(profile_b)
    db = SessionDB(db_path=profile_b / "state.db")
    try:
        scope = make_scope(str(project), profile=current_profile(), root="local")
        result = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="private migration verification passed",
        )
    finally:
        db.close()
        clover_constants.reset_clover_home_override(token)

    assert result.status == "unknown", result
    assert result.source_ids == ()
    assert "private-a" not in result.text


def test_live_session_evidence_older_than_five_minutes_is_unknown(tmp_path):
    import clover_constants
    from agent.workstream_evidence import retrieve_turn_evidence
    from agent.workstreams import current_profile
    from clover_state import SessionDB

    profile, project = tmp_path / "profile", tmp_path / "project"
    profile.mkdir()
    project.mkdir()
    token = clover_constants.set_clover_home_override(profile)
    db = SessionDB(db_path=profile / "state.db")
    moment = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    try:
        _add_session(db, session_id="stale", project=project,
                     content="The deployment is currently running.",
                     timestamp=moment.timestamp() - 360)
        scope = _scope(project, current_profile())
        result = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="is deployment currently running",
            now=moment,
        )
    finally:
        db.close()
        clover_constants.reset_clover_home_override(token)

    assert result.status == "unknown"
    assert result.source_ids == ()
    assert "fresh_evidence" in result.missing_keys


def test_output_is_redacted_and_never_exceeds_utf8_budget(tmp_path):
    import clover_constants
    from agent.workstream_evidence import retrieve_turn_evidence
    from agent.workstreams import current_profile
    from clover_state import SessionDB

    profile, project = tmp_path / "profile", tmp_path / "project"
    profile.mkdir()
    project.mkdir()
    token = clover_constants.set_clover_home_override(profile)
    db = SessionDB(db_path=profile / "state.db")
    try:
        _add_session(
            db, session_id="secret-session", project=project,
            content=("The deployment tests passed. API token: sk-proj-"
                     + "A" * 48 + " " + "café " * 400),
        )
        scope = _scope(project, current_profile())
        result = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="deployment tests passed",
            max_bytes=96,
        )
        too_small = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="deployment tests passed",
            max_bytes=8,
        )
    finally:
        db.close()
        clover_constants.reset_clover_home_override(token)

    assert len(result.text.encode("utf-8")) <= 96
    assert "sk-proj-" not in result.text
    assert result.source_ids
    assert result.source_ids[0] in result.text
    assert too_small.status == "unknown"
    assert too_small.text == ""
    assert too_small.source_ids == ()
    assert "citation_byte_budget" in too_small.missing_keys


def test_session_owner_failure_is_explicit_unknown(tmp_path, monkeypatch):
    import clover_constants
    from agent import workstream_evidence as evidence
    from agent.workstreams import current_profile

    profile, project = tmp_path / "profile", tmp_path / "project"
    profile.mkdir()
    project.mkdir()
    token = clover_constants.set_clover_home_override(profile)

    class BrokenSessionDB:
        def __init__(self, **kwargs):
            raise OSError("fixture owner failure")

    monkeypatch.setattr("clover_state.SessionDB", BrokenSessionDB)
    try:
        scope = _scope(project, current_profile())
        result = evidence.retrieve_turn_evidence(
            _agent(scope), scope=scope, query="deployment tests passed",
        )
    finally:
        clover_constants.reset_clover_home_override(token)

    assert result.status == "unknown"
    assert result.source_ids == ()
    assert "session_search" in result.missing_keys
    assert result.text


def test_cron_owner_conflict_is_partial_and_cited_from_real_execution_ledger(tmp_path, monkeypatch):
    import clover_constants
    import cron.executions as executions
    import cron.jobs as jobs
    from agent.workstream_evidence import retrieve_turn_evidence
    from agent.workstreams import current_profile

    profile, project = tmp_path / "profile", tmp_path / "project"
    profile.mkdir()
    project.mkdir()
    cron_dir = profile / "cron"
    (cron_dir / "output").mkdir(parents=True)
    monkeypatch.setattr(jobs, "CLOVER_DIR", profile)
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", cron_dir / "executions.db")
    token = clover_constants.set_clover_home_override(profile)
    try:
        job = jobs.create_job(
            "deployment tests passed", "every 1h", name="deployment-check",
            workdir=str(project),
        )
        success = executions.create_execution(job["id"], source="test")
        executions.finish_execution(success["id"], success=True)
        failure = executions.create_execution(job["id"], source="test")
        executions.finish_execution(failure["id"], success=False, error="test failure")
        scope = _scope(project, current_profile())
        result = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="deployment tests passed",
        )
    finally:
        clover_constants.reset_clover_home_override(token)

    assert result.status == "partial"
    assert "conflicting_evidence" in result.missing_keys
    assert f"cron-execution:{job['id']}:{success['id']}" in result.source_ids
    assert f"cron-execution:{job['id']}:{failure['id']}" in result.source_ids
    assert "does not establish" in result.text


def test_process_evidence_requires_profile_scoped_parent_session(tmp_path, monkeypatch):
    import clover_constants
    from agent.workstream_evidence import retrieve_turn_evidence
    from agent.workstreams import current_profile
    from clover_state import SessionDB
    from tools.process_registry import ProcessRegistry, ProcessSession

    profile, project = tmp_path / "profile", tmp_path / "project"
    profile.mkdir()
    project.mkdir()
    token = clover_constants.set_clover_home_override(profile)
    db = SessionDB(db_path=profile / "state.db")
    registry = ProcessRegistry()
    try:
        _add_session(db, session_id="owner-session", project=project,
                     content="Check deployment tests.")
        process = ProcessSession(
            id="proc_evidence_001", command="run deployment tests",
            parent_session_id="owner-session", cwd=str(project),
        )
        registry._running[process.id] = process
        monkeypatch.setattr("tools.process_registry.process_registry", registry)
        scope = _scope(project, current_profile())
        result = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="deployment tests",
        )
    finally:
        db.close()
        clover_constants.reset_clover_home_override(token)

    assert "process:proc_evidence_001" in result.source_ids
    assert "Process owner reports running" in result.text


def test_monitor_change_citation_does_not_claim_job_success(tmp_path, monkeypatch):
    import clover_constants
    import cron.jobs as jobs
    from agent.workstream_evidence import retrieve_turn_evidence
    from agent.workstreams import current_profile

    profile, project = tmp_path / "profile", tmp_path / "project"
    profile.mkdir()
    project.mkdir()
    cron_dir = profile / "cron"
    (cron_dir / "output").mkdir(parents=True)
    monkeypatch.setattr(jobs, "CLOVER_DIR", profile)
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    token = clover_constants.set_clover_home_override(profile)
    observed_at = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    try:
        job = jobs.create_job(
            "deployment monitor status", "every 1h", name="monitor-check",
            workdir=str(project), monitor_script="monitor.sh",
        )
        jobs.update_job(job["id"], {
            "monitor_state": {
                "last_output_hash": "test-output-hash",
                "last_changed_at": observed_at.isoformat(),
            }
        })
        scope = _scope(project, current_profile())
        result = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="deployment monitor status",
            now=observed_at,
        )
    finally:
        clover_constants.reset_clover_home_override(token)

    assert f"cron-monitor:{job['id']}" in result.source_ids
    assert "Monitor output changed" in result.text
    assert "outcome is not established" in result.text
    assert result.status == "partial"


def test_cron_process_and_delegation_owner_failures_are_explicit(tmp_path, monkeypatch):
    import clover_constants
    import cron.jobs as jobs
    import tools.async_delegation as delegation
    import tools.process_registry as processes
    from agent.workstream_evidence import retrieve_turn_evidence
    from agent.workstreams import current_profile

    profile, project = tmp_path / "profile", tmp_path / "project"
    profile.mkdir()
    project.mkdir()
    token = clover_constants.set_clover_home_override(profile)

    def fail(*args, **kwargs):
        raise OSError("fixture owner unavailable")

    monkeypatch.setattr(jobs, "list_jobs", fail)
    monkeypatch.setattr(processes.process_registry, "list_sessions", fail)
    monkeypatch.setattr(delegation, "list_async_delegations", fail)
    try:
        scope = _scope(project, current_profile())
        result = retrieve_turn_evidence(
            _agent(scope), scope=scope, query="deployment tests passed",
        )
    finally:
        clover_constants.reset_clover_home_override(token)

    assert result.status == "unknown"
    assert result.source_ids == ()
    assert "cron_evidence" in result.missing_keys
    assert "process_state" in result.missing_keys
    assert "delegation_receipt" in result.missing_keys

"""Scheduler-owned workstream monitor occurrence and safe claim tests."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from threading import Barrier


def _setup_scope(monkeypatch, tmp_path, *, eligible=True):
    from agent import workstreams
    from cron import notepad

    profile = tmp_path / "profile"
    project = tmp_path / "project"
    profile.mkdir()
    project.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(profile))
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", tmp_path / "notepad.db")
    job_id = "monitor-test"
    settings = workstreams.load_settings(
        {
            "proactive": {
                "enabled": True,
                "eligible_jobs": [job_id] if eligible else [],
            }
        }
    )
    monkeypatch.setattr(workstreams, "load_settings", lambda: settings)
    scope = workstreams.make_scope(str(project), audience="private", root="")
    record = notepad.create_workstream(
        scope,
        kind="obligation",
        title="Verify monitor change",
        summary="Inspect the changed monitor output",
        fingerprint="monitor-change",
        approved=True,
    )
    return workstreams, notepad, scope, record, project, job_id


def test_linked_monitor_occurrence_survives_restart_and_has_one_claim(
    monkeypatch, tmp_path
):
    from cron import scheduler

    _workstreams, notepad, scope, record, project, job_id = _setup_scope(
        monkeypatch, tmp_path
    )
    job = {"id": job_id, "workdir": str(project), "workstream_id": record["workstream_id"]}
    outcome = SimpleNamespace(
        context_block="## MONITOR CHANGE DETECTED\n\nrelease gate changed"
    )

    occurrence = scheduler._record_workstream_monitor_occurrence(job, outcome)
    assert occurrence is not None
    assert occurrence["state"] == "pending"

    # A restarted scheduler replays the same deterministic event key; SQLite
    # returns the single existing pending occurrence, not a second event.
    replay = scheduler._record_workstream_monitor_occurrence(job, outcome)
    assert replay["occurrence_key"] == occurrence["occurrence_key"]
    rows = notepad.pending_occurrences(scope, workstream_id=record["workstream_id"])
    assert len(rows) == 1

    barrier = Barrier(3)

    def claim(index):
        barrier.wait()
        return notepad.claim_occurrence(
            scope,
            record["workstream_id"],
            occurrence_key=occurrence["occurrence_key"],
            claimer=f"worker-{index}",
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(claim, range(3)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    owner = winners[0]["claimer"]
    notepad.settle_occurrence(
        scope,
        record["workstream_id"],
        occurrence_key=occurrence["occurrence_key"],
        claimer=owner,
        state="unknown",
        detail="external effect not reconciled",
    )
    assert notepad.claim_occurrence(
        scope,
        record["workstream_id"],
        occurrence_key=occurrence["occurrence_key"],
        claimer="retry-must-not-run",
    ) is None
    assert notepad.pending_occurrences(scope, workstream_id=record["workstream_id"]) == []


def test_unapproved_or_ineligible_monitor_does_not_create_occurrence(
    monkeypatch, tmp_path
):
    from cron import scheduler

    _workstreams, notepad, scope, record, project, job_id = _setup_scope(
        monkeypatch, tmp_path, eligible=False
    )
    job = {"id": job_id, "workdir": str(project), "workstream_id": record["workstream_id"]}
    outcome = SimpleNamespace(context_block="monitor change")

    assert scheduler._record_workstream_monitor_occurrence(job, outcome) is None
    assert notepad.pending_occurrences(scope, workstream_id=record["workstream_id"]) == []


def test_monitor_occurrence_without_runtime_link_is_not_guessed(monkeypatch, tmp_path):
    from cron import scheduler

    _workstreams, notepad, scope, _record, project, job_id = _setup_scope(
        monkeypatch, tmp_path
    )
    job = {"id": job_id, "workdir": str(project)}

    assert (
        scheduler._record_workstream_monitor_occurrence(
            job, SimpleNamespace(context_block="monitor change")
        )
        is None
    )
    assert notepad.pending_occurrences(scope) == []


def test_run_job_does_not_turn_monitor_evidence_into_agent_authority(
    monkeypatch, tmp_path
):
    from cron import jobs, monitor, scheduler

    _workstreams, notepad, scope, record, project, job_id = _setup_scope(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(jobs, "job_payload_is_empty", lambda _job: False)
    dispatches = []
    monkeypatch.setattr(
        scheduler,
        "_build_job_prompt",
        lambda *_args, **_kwargs: dispatches.append("prompt") or "prompt",
    )
    monkeypatch.setattr(monitor, "job_has_monitor", lambda _job: True)
    monkeypatch.setattr(
        monitor,
        "check_monitor",
        lambda _job: SimpleNamespace(
            ok=True,
            changed=True,
            first_run=False,
            context_block="## MONITOR CHANGE DETECTED\n\nrelease gate changed",
            error=None,
        ),
    )
    job = {
        "id": job_id,
        "name": "monitor",
        "prompt": "Inspect",
        "workdir": str(project),
        "workstream_id": record["workstream_id"],
        "monitor_script": "monitor.sh",
    }

    success, _doc, final, error = scheduler.run_job(job)

    assert success is False
    assert final == scheduler.SILENT_MARKER
    assert error and "no authorized runtime continuation route" in error
    assert dispatches == []
    assert notepad.pending_occurrences(scope, workstream_id=record["workstream_id"]) == []
    receipts = notepad.list_occurrences(scope, workstream_id=record["workstream_id"])
    assert len(receipts) == 1
    assert receipts[0]["state"] == "unknown"


def test_monitor_hash_persistence_precedes_occurrence_and_exposes_crash_gap(
    monkeypatch, tmp_path
):
    from cron import jobs, monitor

    _workstreams, notepad, scope, record, project, _job_id = _setup_scope(
        monkeypatch, tmp_path
    )
    home = tmp_path / "profile"
    scripts = home / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "monitor.sh").write_text("printf 'gate green\\n'\n", encoding="utf-8")
    job = jobs.create_job(
        prompt="Inspect monitor result",
        schedule="every 5m",
        monitor_script="monitor.sh",
        deliver="local",
    )
    settings = _workstreams.ProactiveSettings(enabled=True, eligible_jobs=(job["id"],))
    monkeypatch.setattr(_workstreams, "load_settings", lambda: settings)
    job["workdir"] = str(project)
    job["workstream_id"] = record["workstream_id"]

    detected = monitor.check_monitor(job)
    assert detected.ok and detected.changed
    # Simulate process loss after the monitor hash is durable but before
    # run_job consumes the outcome. The occurrence must already be durable.
    pending_before_restart = notepad.pending_occurrences(
        scope, workstream_id=record["workstream_id"]
    )
    assert len(pending_before_restart) == 1
    after_restart = jobs.get_job(job["id"])
    assert after_restart is not None
    after_restart["workdir"] = str(project)
    after_restart["workstream_id"] = record["workstream_id"]
    replay = monitor.check_monitor(after_restart)

    assert replay.ok and not replay.changed
    pending_after_restart = notepad.pending_occurrences(
        scope, workstream_id=record["workstream_id"]
    )
    assert len(pending_after_restart) == 1
    assert pending_after_restart[0]["occurrence_key"] == pending_before_restart[0]["occurrence_key"]

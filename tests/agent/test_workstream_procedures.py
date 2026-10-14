"""Reviewed procedure proposals from repeated verified success (PLAN-v1 #5).

Real SQLite ledger, the real executions ledger, and the real
``tools.write_approval`` pending store in a scratch CLOVER_HOME.  A candidate
needs >=3 distinct verified executions across >=2 sessions with transfer
steps; it is *staged* (never installed) via ``stage_write('skills', ...)``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import curator
from agent import workstreams as ws
from cron import notepad as np
from tools import write_approval as wa

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
ON = ws.ProactiveSettings(enabled=True, mode="active", procedures_enabled=True)
STEPS = "1) list artifacts 2) run checksum audit 3) compare manifest 4) write report"


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(np, "NOTEPAD_FILE", tmp_path / "cron" / "notepad.db")
    import cron.executions as ex

    monkeypatch.setattr(ex, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return tmp_path


def sc(project="proj-a", audience="private", root="root-1"):
    return ws.make_scope(project, audience=audience, root=root, profile="/home/p1")


def _exec(job="audit-job", ok=True):
    """A completed (or failed) row in the real cron executions ledger."""
    import cron.executions as ex

    rec = ex.create_execution(job, source="test")
    ex.mark_execution_running(rec["id"])
    ex.finish_execution(rec["id"], success=ok, error=None if ok else "boom")
    return rec["id"]


_N = {"n": 0}


def _session(sid, *, parent=None):
    """A real SessionDB session row in the scratch CLOVER_HOME."""
    from clover_state import SessionDB

    db = SessionDB()
    try:
        if db.get_session(sid) is None:
            db.create_session(sid, "cli", parent_session_id=parent)
    finally:
        db.close()
    return sid


def _deleg(*, status="completed", complete=True, deliver=True, did=None):
    """A real durable delegation row; ``deliver`` = a consumer claimed it."""
    import time

    import tools.async_delegation as ad

    _N["n"] += 1
    did = did or f"del-{_N['n']:04d}"
    ad._persist_dispatch({"delegation_id": did, "dispatched_at": time.time(),
                          "goal": "g", "session_key": "s"})
    if complete:
        ad._persist_completion({"delegation_id": did, "status": status},
                               {"status": status, "summary": "ok"})
        if deliver:
            assert ad.mark_completion_delivered(did)
    return did


def _wid(scope, name="release audit runs"):
    return np.create_workstream(scope, kind="progress", title=name, summary=name,
                                fingerprint=name, now=NOW)["workstream_id"]


def run(scope, wid, session, *, key="release-audit", ok=True, steps=STEPS,
        delegation_id=None, minutes=0, settings=ON, deliver=True, make_session=True):
    """One run through the production verifier. Returns (evidence|None, id)."""
    if make_session and session:
        _session(session)
    did = delegation_id or _deleg(status="completed" if ok else "failed", deliver=deliver)
    return ws.record_verified_run(
        scope, wid, procedure_key=key, proof=ws.DelegationProof(did),
        session_id=session, steps=steps, settings=settings,
        now=NOW + timedelta(minutes=minutes),
    ), did


def pending():
    return wa.list_pending("skills")


class TestRecordVerifiedRun:
    def test_requires_owner_confirmed_consumed_success_and_a_real_session(self):
        scope = sc()
        wid = _wid(scope)
        good, did = run(scope, wid, "s1")
        assert good["status"] == "verified" and good["procedure_key"] == "release-audit"
        assert good["source_id"] == did and good["session_id"] == "s1"
        assert good["trust"] == "owner_session" and good["source_kind"] == "delegation"
        kinds = {r["kind"]: r["owner"] for r in np.list_receipts(scope, wid)}
        assert kinds == {"executed": "execution", "delivered": "delivery"}

    def test_transport_only_cron_completion_is_never_verified_success(self):
        scope = sc()
        wid = _wid(scope)
        _session("s1")
        eid = _exec()
        got = ws.record_verified_run(
            scope, wid, procedure_key="release-audit", proof=ws.ExecutedProof("audit-job", eid),
            session_id="s1", steps=STEPS, settings=ON, now=NOW)
        assert got is None
        assert np.list_evidence(scope, wid) == [] and np.list_receipts(scope, wid) == []

    @pytest.mark.parametrize("variant", ["failed", "running", "unclaimed", "missing"])
    def test_failed_unknown_unconsumed_or_missing_outcomes_do_not_count(self, variant):
        scope = sc()
        wid = _wid(scope)
        did = {
            "failed": lambda: _deleg(status="failed"),
            "running": lambda: _deleg(complete=False),
            "unclaimed": lambda: _deleg(deliver=False),
            "missing": lambda: "never-dispatched",
        }[variant]()
        got, _ = run(scope, wid, "s1", delegation_id=did)
        assert got is None
        assert np.list_evidence(scope, wid) == [] and np.list_receipts(scope, wid) == []

    @pytest.mark.parametrize("session", ["", "invented-session"])
    def test_missing_or_fabricated_session_never_counts(self, session):
        scope = sc()
        wid = _wid(scope)
        got, _ = run(scope, wid, session, make_session=False)
        assert got is None and np.list_evidence(scope, wid) == []

    def test_compaction_rotated_segments_are_one_conversation(self):
        scope = sc()
        wid = _wid(scope)
        _session("conv-1")
        _session("conv-1b", parent="conv-1")      # context compression rotation
        a, _ = run(scope, wid, "conv-1")
        b, _ = run(scope, wid, "conv-1b")
        assert a["session_id"] == b["session_id"] == "conv-1"

    def test_same_delegation_replayed_is_one_evidence_row(self):
        scope = sc()
        wid = _wid(scope)
        first, did = run(scope, wid, "s1")
        again, _ = run(scope, wid, "s2", delegation_id=did)
        assert again["evidence_id"] == first["evidence_id"]
        assert len(np.list_evidence(scope, wid)) == 1
        assert len(np.list_receipts(scope, wid, kinds=["executed"])) == 1

    def test_disabled_is_inert(self):
        scope = sc()
        wid = _wid(scope)
        got, _ = run(scope, wid, "s1", settings=ws.ProactiveSettings())
        assert got is None and np.list_evidence(scope, wid) == []


class TestThreshold:
    def _three(self, scope, wid, sessions=("s1", "s1", "s2")):
        for i, s in enumerate(sessions):
            run(scope, wid, s, minutes=i)

    def test_legacy_delegation_completions_never_stage_a_draft(self, store):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        cands = curator.propose_procedure_candidates(scope, ON, now=NOW + timedelta(hours=1))
        assert cands == []
        assert pending() == []

    def test_legacy_replay_does_not_stage(self):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        run(scope, wid, "s3", minutes=9)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert pending() == []

    def test_legacy_rows_never_create_a_proposal_to_reject(self):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert pending() == []

    def test_fewer_than_three_executions_yield_nothing(self):
        scope = sc()
        wid = _wid(scope)
        run(scope, wid, "s1")
        run(scope, wid, "s2", minutes=1)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert pending() == []

    def test_same_execution_repeated_counts_once(self):
        scope = sc()
        wid = _wid(scope)
        _, did = run(scope, wid, "s1")
        for s in ("s2", "s3"):
            run(scope, wid, s, delegation_id=did)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []

    def test_failed_runs_never_count(self):
        scope = sc()
        wid = _wid(scope)
        run(scope, wid, "s1")
        run(scope, wid, "s2", minutes=1)
        for i in range(3):
            got, _ = run(scope, wid, f"s{i+3}", ok=False, minutes=2 + i)
            assert got is None
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []

    def test_single_session_is_not_enough(self):
        scope = sc()
        wid = _wid(scope)
        for i in range(4):
            run(scope, wid, "only-session", minutes=i)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []

    def test_no_transfer_steps_no_candidate(self):
        scope = sc()
        wid = _wid(scope)
        for i, s in enumerate(("s1", "s2", "s3")):
            run(scope, wid, s, steps="", minutes=i)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []

    def test_runs_without_owner_session_trust_do_not_count(self):
        scope = sc()
        wid = _wid(scope)
        for i, sess in enumerate(("s1", "s2", "s3")):
            did = _deleg()
            np.add_receipt(scope, wid, kind="executed", owner="execution", source_id=did, now=NOW)
            np.add_evidence(scope, wid, source_kind="delegation", source_id=did,
                            status="verified", key="procedure_run",
                            procedure_key="release-audit", session_id=sess, summary=STEPS,
                            event_at=NOW.isoformat(), now=NOW)   # default trust: 'runtime'
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []

    def test_evidence_without_owner_receipt_does_not_count(self):
        scope = sc()
        wid = _wid(scope)
        for i, s in enumerate(("s1", "s2", "s3")):
            np.add_evidence(scope, wid, source_kind="execution", source_id=f"fake{i}",
                            status="verified", key="procedure_run",
                            procedure_key="release-audit", session_id=s,
                            summary=STEPS, event_at=NOW.isoformat(), now=NOW)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []

    def test_scopes_do_not_pool_runs(self):
        a, b = sc(project="a"), sc(project="b")
        wa_, wb_ = _wid(a), _wid(b)
        run(a, wa_, "s1")
        run(a, wa_, "s2", minutes=1)
        run(b, wb_, "s3", minutes=2)
        assert curator.propose_procedure_candidates(a, ON, now=NOW) == []
        assert curator.propose_procedure_candidates(b, ON, now=NOW) == []

    def test_distinct_procedure_keys_do_not_pool(self):
        scope = sc()
        wid = _wid(scope)
        run(scope, wid, "s1", key="k1")
        run(scope, wid, "s2", key="k2", minutes=1)
        run(scope, wid, "s3", key="k1", minutes=2)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []

    def test_disabled_or_nonprivate_stages_nothing(self):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        off = ws.ProactiveSettings(enabled=True, procedures_enabled=False)
        assert curator.propose_procedure_candidates(scope, off, now=NOW) == []
        assert curator.propose_procedure_candidates(scope, ws.ProactiveSettings(), now=NOW) == []
        grp = sc(audience="group:c1")
        assert curator.propose_procedure_candidates(grp, ON, now=NOW) == []
        assert pending() == []

    def test_settings_cannot_lower_minimum_or_admit_legacy_rows(self):
        scope = sc()
        wid = _wid(scope)
        run(scope, wid, "s1")
        run(scope, wid, "s2", minutes=1)
        loose = ws.ProactiveSettings(enabled=True, mode="active", procedures_enabled=True,
                                     procedures_min_verified_runs=2, procedures_min_sessions=2)
        assert curator.propose_procedure_candidates(scope, loose, now=NOW) == []

    def test_legacy_rows_never_reach_stage_boundary(self):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)

        def boom(*a, **k):
            raise OSError("disk full")

        assert curator.propose_procedure_candidates(scope, ON, now=NOW, stage=boom) == []
        proc = np.list_workstreams(scope, kinds=["procedure"])
        assert proc == [] or np.list_receipts(scope, proc[0]["workstream_id"],
                                               kinds=["proposed"]) == []
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []

    def test_legacy_rows_cannot_create_a_stage_crash_window(self):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        real = wa.stage_write
        c1 = curator.propose_procedure_candidates(scope, ON, now=NOW)
        assert c1 == []
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert pending() == [] and real is wa.stage_write

    def test_existing_skill_of_same_name_is_not_overwritten(self, store):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        name = curator.procedure_skill_name("release-audit")
        from clover_constants import get_clover_home

        d = get_clover_home() / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: x\ndescription: x.\n---\nbody\n", encoding="utf-8")
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert pending() == []

    def test_candidate_is_never_applied_by_intake(self, store, monkeypatch):
        import tools.skill_manager_tool as smt

        called = []
        monkeypatch.setattr(smt, "skill_manage", lambda *a, **k: called.append(1))
        monkeypatch.setattr(smt, "apply_skill_pending", lambda *a, **k: called.append(1))
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert called == []


class TestCuratorCadence:
    """The existing curator pass is the production caller of procedure intake."""

    def _three_runs(self, scope):
        wid = _wid(scope)
        for i, s in enumerate(("s1", "s1", "s2")):
            run(scope, wid, s, minutes=i)

    def test_enumerates_only_scopes_with_procedure_evidence(self):
        a, b = sc(project="a"), sc(project="b")
        self._three_runs(a)
        _wid(b)   # a scope with records but no procedure runs
        got = np.list_procedure_scopes()
        assert [(r["project"], r["audience"], r["root"]) for r in got] == [("a", "private", "root-1")]

    def test_maybe_run_curator_rejects_legacy_delegation_evidence(self, monkeypatch):
        scope = sc(project="p", audience="private", root="r")
        scope = ws.Scope(profile=ws.current_profile(), project="p", audience="private", root="r")
        self._three_runs(scope)
        monkeypatch.setattr(curator, "should_run_now", lambda *a, **k: True)
        monkeypatch.setattr(curator, "run_curator_review", lambda **k: {"ran": True})
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)
        assert curator.maybe_run_curator() == {"ran": True}
        assert pending() == []
        curator.maybe_run_curator()
        assert pending() == []

    def test_default_settings_stage_nothing_and_touch_no_db(self, monkeypatch, store):
        monkeypatch.setattr(curator, "should_run_now", lambda *a, **k: True)
        monkeypatch.setattr(curator, "run_curator_review", lambda **k: {"ran": True})
        assert curator.maybe_run_curator() == {"ran": True}
        assert pending() == []
        assert not (store / "cron" / "notepad.db").exists()

    def test_intake_failure_never_blocks_the_curator_pass(self, monkeypatch):
        monkeypatch.setattr(curator, "should_run_now", lambda *a, **k: True)
        monkeypatch.setattr(curator, "run_curator_review", lambda **k: {"ran": True})
        monkeypatch.setattr(curator, "run_procedure_intake",
                            lambda **k: (_ for _ in ()).throw(RuntimeError("db locked")))
        assert curator.maybe_run_curator() == {"ran": True}


class TestCronCannotProveProcedures:
    """The cron executions ledger records transport status and carries no
    session identity: it must never stage a draft, however many runs."""

    @pytest.fixture
    def job(self, monkeypatch, tmp_path):
        import cron.jobs as jobs_mod

        home = tmp_path / "home"
        (home / "cron" / "output").mkdir(parents=True)
        monkeypatch.setattr(jobs_mod, "CLOVER_DIR", home)
        monkeypatch.setattr(jobs_mod, "CRON_DIR", home / "cron")
        monkeypatch.setattr(jobs_mod, "JOBS_FILE", home / "cron" / "jobs.json")
        monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", home / "cron" / "output")
        proj = tmp_path / "proj"
        proj.mkdir()
        return jobs_mod.create_job("Audit release artifacts and write the report.",
                                   "every 1d", name="release-audit", workdir=str(proj))

    def test_many_completed_executions_of_an_enrolled_job_stage_nothing(self, job, store):
        for _ in range(5):
            _exec(job["id"])
        s = ws.ProactiveSettings(enabled=True, mode="active", procedures_enabled=True,
                                 eligible_jobs=(job["id"],))
        assert curator.run_procedure_intake(settings=s, now=NOW) == []
        assert pending() == []
        assert not hasattr(ws, "collect_cron_verified_runs")
        # nothing was even recorded for the job
        assert np.list_procedure_scopes() == []
        assert curator.maybe_run_curator is not None


class TestDelegationProducerChain:
    """end_turn (production) -> delegation ledger + SessionDB owners -> ledger
    evidence -> curator intake -> stage_write('skills')."""

    GOAL = "Audit the release artifacts: list them, checksum each, compare the manifest, write the report"

    @pytest.fixture(autouse=True)
    def host(self, monkeypatch, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(proj))
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)

    def _agent(self, session_id, *specs, make_session=True):
        import types

        from agent import foreground_time_limit as fl
        from clover_state import SessionDB

        if make_session and session_id:
            _session(session_id)
        own = {d: fl.DispatchReceipt(delegation_id=d, seq=i + 1, goals=tuple(g), turn_id="t1")
               for i, (d, g) in enumerate(specs)}
        return types.SimpleNamespace(
            platform="cli", session_id=session_id, _session_db=SessionDB(read_only=True),
            _fg_own_dispatches=own, _fg_dispatch_seq=len(own), _wl_floor=0,
            _wl_completion_ids=tuple(d for d, _ in specs))

    def _end(self, session_id, *specs, **kw):
        from agent import work_lifecycle as wl

        agent = self._agent(session_id, *specs, **kw)
        try:
            wl.end_turn(agent)
        finally:
            agent._session_db.close()
        return ws.scope_for_agent(agent)

    def test_delegation_transport_chain_never_stages_a_procedure(self):
        ids = [_deleg() for _ in range(3)]
        scope = self._end("conv-A", (ids[0], [self.GOAL]))
        self._end("conv-A", (ids[1], [self.GOAL]))
        self._end("conv-B", (ids[2], [self.GOAL]))
        assert curator.run_procedure_intake(settings=ON, now=NOW) == []
        assert pending() == []
        runs = [r for r in np.list_workstreams(scope, kinds=["progress"])
                if r["fingerprint"] == "delegation-runs"]
        assert runs == []
        self._end("conv-B", (ids[2], [self.GOAL]))
        assert curator.run_procedure_intake(settings=ON, now=NOW) == []
        assert pending() == []

    def test_one_session_however_many_runs_never_qualifies(self):
        for _ in range(4):
            self._end("conv-A", (_deleg(), [self.GOAL]))
        assert curator.run_procedure_intake(settings=ON, now=NOW) == []
        assert pending() == []

    def test_failed_unconsumed_and_unknown_outcomes_do_not_increment(self):
        self._end("conv-A", (_deleg(), [self.GOAL]))
        self._end("conv-B", (_deleg(), [self.GOAL]))
        for bad in (_deleg(status="failed"), _deleg(deliver=False), _deleg(complete=False),
                    "never-dispatched"):
            self._end("conv-C", (bad, [self.GOAL]))
        assert curator.run_procedure_intake(settings=ON, now=NOW) == []
        assert pending() == []

    def test_fabricated_or_missing_session_does_not_count_as_a_session(self):
        self._end("conv-A", (_deleg(), [self.GOAL]))
        self._end("conv-A", (_deleg(), [self.GOAL]))
        self._end("never-created", (_deleg(), [self.GOAL]), make_session=False)
        self._end("", (_deleg(), [self.GOAL]), make_session=False)
        assert curator.run_procedure_intake(settings=ON, now=NOW) == []

    def test_same_delegation_replayed_from_other_sessions_counts_once(self):
        did = _deleg()
        for sess in ("conv-A", "conv-B", "conv-C"):
            self._end(sess, (did, [self.GOAL]))
        assert curator.run_procedure_intake(settings=ON, now=NOW) == []

    def test_different_goals_and_batches_do_not_pool(self):
        self._end("conv-A", (_deleg(), ["Rotate the signing keys for the staging cluster"]))
        self._end("conv-B", (_deleg(), ["Rebuild the documentation search index fully"]))
        self._end("conv-C", (_deleg(), [self.GOAL, "second batch task goal text"]))
        self._end("conv-D", (_deleg(), [self.GOAL, "second batch task goal text"]))
        assert curator.run_procedure_intake(settings=ON, now=NOW) == []

    def test_disabled_procedures_record_nothing(self, monkeypatch, store):
        off = ws.ProactiveSettings(enabled=True, mode="active", procedures_enabled=False)
        monkeypatch.setattr(ws, "load_settings", lambda config=None: off)
        for sess in ("conv-A", "conv-B", "conv-C"):
            self._end(sess, (_deleg(), [self.GOAL]))
        assert np.list_procedure_scopes() == []
        assert curator.run_procedure_intake(settings=off, now=NOW) == []

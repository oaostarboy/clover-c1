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
    import cron.executions as ex

    rec = ex.create_execution(job, source="test")
    ex.mark_execution_running(rec["id"])
    ex.finish_execution(rec["id"], success=ok, error=None if ok else "boom")
    return rec["id"]


def _wid(scope, name="release audit runs"):
    return np.create_workstream(scope, kind="progress", title=name, summary=name,
                                fingerprint=name, now=NOW)["workstream_id"]


def run(scope, wid, session, *, key="release-audit", ok=True, steps=STEPS,
        execution_id=None, minutes=0, settings=ON):
    eid = execution_id or _exec(ok=ok)
    return ws.record_verified_run(
        scope, wid, procedure_key=key,
        proof=ws.ExecutedProof("audit-job", eid), session_id=session,
        steps=steps, settings=settings, now=NOW + timedelta(minutes=minutes),
    ), eid


def pending():
    return wa.list_pending("skills")


class TestRecordVerifiedRun:
    def test_requires_owner_confirmed_execution(self):
        scope = sc()
        wid = _wid(scope)
        good, eid = run(scope, wid, "s1")
        assert good["status"] == "verified" and good["procedure_key"] == "release-audit"
        assert good["source_id"] == eid and good["session_id"] == "s1"
        assert [r["kind"] for r in np.list_receipts(scope, wid)] == ["executed"]
        bad, _ = run(scope, wid, "s1", ok=False)
        assert bad is None
        none = ws.record_verified_run(
            scope, wid, procedure_key="release-audit",
            proof=ws.ExecutedProof("audit-job", "invented"), session_id="s1",
            steps=STEPS, settings=ON, now=NOW)
        assert none is None
        assert len(np.list_evidence(scope, wid)) == 1

    def test_disabled_is_inert(self):
        scope = sc()
        wid = _wid(scope)
        got, _ = run(scope, wid, "s1", settings=ws.ProactiveSettings())
        assert got is None and np.list_evidence(scope, wid) == []


class TestThreshold:
    def _three(self, scope, wid, sessions=("s1", "s1", "s2")):
        for i, s in enumerate(sessions):
            run(scope, wid, s, minutes=i)

    def test_three_executions_two_sessions_stage_exactly_one_reviewable_draft(self, store):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        cands = curator.propose_procedure_candidates(scope, ON, now=NOW + timedelta(hours=1))
        assert len(cands) == 1
        c = cands[0]
        assert c["procedure_key"] == "release-audit"
        assert len(c["execution_ids"]) == 3 and len(c["session_ids"]) == 2
        recs = pending()
        assert len(recs) == 1
        rec = recs[0]
        assert rec["id"] == c["pending_id"] and rec["subsystem"] == "skills"
        assert rec["action"] == "create" and rec["origin"] == "background_review"
        assert set(rec["payload"]) == {"action", "name", "content"}
        content = rec["payload"]["content"]
        for eid in c["execution_ids"]:
            assert eid in content            # attribution to the verified runs
        assert "list artifacts" in content   # the transferable steps
        assert "proactive" in rec["summary"].lower()
        from tools.skill_manager_tool import _validate_frontmatter, _validate_name

        assert _validate_name(rec["payload"]["name"]) is None
        assert _validate_frontmatter(content, new_skill=True) is None
        # staged, never installed
        from clover_constants import get_clover_home

        skills_dir = get_clover_home() / "skills"
        assert not skills_dir.exists() or not any(skills_dir.rglob("SKILL.md"))
        # typed 'proposed' receipt with the pending id, on a procedure record
        proc = np.list_workstreams(scope, kinds=["procedure"])
        assert len(proc) == 1
        rc = np.list_receipts(scope, proc[0]["workstream_id"], kinds=["proposed"])
        assert [r["source_id"] for r in rc] == [f"pending:{rec['id']}"]

    def test_replay_does_not_stage_again(self):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        assert len(curator.propose_procedure_candidates(scope, ON, now=NOW)) == 1
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        run(scope, wid, "s3", minutes=9)
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert len(pending()) == 1

    def test_rejected_proposal_is_not_nagged_again(self):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        c = curator.propose_procedure_candidates(scope, ON, now=NOW)[0]
        assert wa.discard_pending("skills", c["pending_id"])
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
        _, eid = run(scope, wid, "s1")
        for s in ("s2", "s3"):
            run(scope, wid, s, execution_id=eid)
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

    def test_thresholds_come_from_settings(self):
        scope = sc()
        wid = _wid(scope)
        run(scope, wid, "s1")
        run(scope, wid, "s2", minutes=1)
        loose = ws.ProactiveSettings(enabled=True, mode="active", procedures_enabled=True,
                                     procedures_min_verified_runs=2, procedures_min_sessions=2)
        assert len(curator.propose_procedure_candidates(scope, loose, now=NOW)) == 1

    def test_stage_failure_leaves_no_receipt_so_next_run_retries(self):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)

        def boom(*a, **k):
            raise OSError("disk full")

        assert curator.propose_procedure_candidates(scope, ON, now=NOW, stage=boom) == []
        proc = np.list_workstreams(scope, kinds=["procedure"])
        assert proc == [] or np.list_receipts(scope, proc[0]["workstream_id"],
                                               kinds=["proposed"]) == []
        assert len(curator.propose_procedure_candidates(scope, ON, now=NOW)) == 1

    def test_crash_after_stage_before_receipt_does_not_double_stage(self):
        scope = sc()
        wid = _wid(scope)
        self._three(scope, wid)
        real = wa.stage_write
        c1 = curator.propose_procedure_candidates(scope, ON, now=NOW)
        assert len(c1) == 1
        # simulate losing the receipt (crash): remove ledger knowledge, pending remains
        proc = np.list_workstreams(scope, kinds=["procedure"])[0]
        import sqlite3

        conn = sqlite3.connect(np._notepad_path())
        conn.execute("DELETE FROM work_receipts WHERE kind='proposed'")
        conn.commit()
        conn.close()
        assert curator.propose_procedure_candidates(scope, ON, now=NOW) == []
        assert len(pending()) == 1 and real is wa.stage_write
        assert proc["kind"] == "procedure"

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
        assert len(curator.propose_procedure_candidates(scope, ON, now=NOW)) == 1
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

    def test_maybe_run_curator_stages_drafts_for_enrolled_scopes(self, monkeypatch):
        scope = sc(project="p", audience="private", root="r")
        scope = ws.Scope(profile=ws.current_profile(), project="p", audience="private", root="r")
        self._three_runs(scope)
        monkeypatch.setattr(curator, "should_run_now", lambda *a, **k: True)
        monkeypatch.setattr(curator, "run_curator_review", lambda **k: {"ran": True})
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)
        assert curator.maybe_run_curator() == {"ran": True}
        recs = pending()
        assert len(recs) == 1 and recs[0]["action"] == "create"
        # second pass is a no-op (typed proposed receipt)
        curator.maybe_run_curator()
        assert len(pending()) == 1

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

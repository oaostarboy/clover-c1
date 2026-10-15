"""Workstream policy owner (PLAN-v1): settings, typed owner receipts, unfinished
work, corrections, feedback/timing/novelty gates, evidence sufficiency and the
bounded context selector.

Real SQLite in a scratch CLOVER_HOME, the real executions ledger and the real
durable async-delegation ledger; no mocks of the owners under test.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent import workstreams as ws
from cron import notepad as np

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
ON = ws.ProactiveSettings(enabled=True, mode="active", recall_enabled=True,
                          suggestions_enabled=True)


class HumanDouble:
    """Contract-conformance double for the trusted human-origin read API.

    The central owner has NOT published that API (see
    TRUSTED-HUMAN-ORIGIN-INTERFACE.md). This double returns exactly the
    fields the interface requires so the consumer's binding/idempotency logic
    can be exercised; it is not evidence that any real ingress is trusted.
    """

    def __init__(self):
        self.rows = {}
        self.n = 0

    def issue(self, db, session_id, *, profile=None, origin="human", consumed=False):
        row = [r for r in db.get_messages(session_id) if r["role"] == "user"][-1]
        self.n += 1
        got = ws.HumanOrigin(profile or ws.current_profile(), session_id, row["id"],
                             f"evt-{self.n}", origin, consumed)
        self.rows[(session_id, row["id"])] = got
        return got

    def reader(self, db, session_id, message_row_id):
        return self.rows.get((session_id, message_row_id))


H = HumanDouble()


@pytest.fixture(autouse=True)
def _reset_human_double():
    H.rows.clear()
    H.n = 0
    yield


def horigin():
    """A fresh double-issued receipt bound to a real SessionDB user row."""
    from clover_state import SessionDB

    db = SessionDB()
    try:
        sid = "co-sess"
        if db.get_session(sid) is None:
            db.create_session(sid, "cli")
        db.append_message(sid, "user", "ok")
        return H.issue(db, sid)
    finally:
        db.close()


def hfb(scope, wid, kind, **kw):
    """Explicit feedback carried by an issued (double) human-origin receipt."""
    from clover_state import SessionDB

    db = SessionDB()
    try:
        sid = "fb-sess"
        if db.get_session(sid) is None:
            db.create_session(sid, "cli")
        db.append_message(sid, "user", "ok")
        origin = H.issue(db, sid)
    finally:
        db.close()
    kw.setdefault("settings", ON)
    kw.setdefault("now", NOW)
    return ws.record_feedback(scope, wid, kind=kind, origin=origin,
                              human_reader=H.reader, **kw)


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(np, "NOTEPAD_FILE", tmp_path / "cron" / "notepad.db")
    import cron.executions as ex

    monkeypatch.setattr(ex, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return tmp_path


def sc(project="proj-a", audience="private", root="root-1"):
    return ws.make_scope(project, audience=audience, root=root, profile="/home/p1")


def out(deliverable, status, source_id, **kw):
    return ws.TypedOutcome(
        deliverable=deliverable, status=status,
        source_kind=kw.pop("source_kind", "delegation"),
        source_id=source_id, **kw,
    )


# ── settings ───────────────────────────────────────────────────────────────


class TestSettings:
    def test_defaults_are_all_disabled(self):
        s = ws.load_settings({})
        assert s.enabled is False and s.active is False
        assert s.mode == "shadow"
        assert (s.recall_enabled, s.suggestions_enabled, s.procedures_enabled,
                s.compaction_audit_enabled) == (False, False, False, False)
        assert s.private_only is True
        assert (s.threads_max_items, s.threads_max_context_bytes,
                s.threads_retention_days) == (16, 4096, 30)
        assert (s.recall_max_items, s.recall_max_bytes) == (3, 1200)
        assert (s.suggestions_cooldown_seconds, s.suggestions_max_per_session) == (3600, 1)
        assert (s.procedures_min_verified_runs, s.procedures_min_sessions) == (3, 2)
        assert (s.aux_max_calls_per_event, s.aux_max_calls_per_day,
                s.aux_max_output_tokens) == (1, 4, 512)
        assert s.compaction_audit_max_repairs == 0

    def test_shipped_default_config_matches_dataclass_defaults(self):
        from clover_cli.config_defaults import DEFAULT_CONFIG

        shipped = ws.load_settings(DEFAULT_CONFIG)
        assert shipped == ws.ProactiveSettings()
        assert DEFAULT_CONFIG["proactive"]["enabled"] is False

    def test_config_values_parsed_and_junk_falls_back(self):
        s = ws.load_settings({"proactive": {
            "enabled": True, "mode": "active", "eligible_jobs": ["j1", "j2"],
            "threads": {"max_items": 5, "max_context_bytes": "junk"},
            "recall": {"enabled": True, "max_items": -3},
        }})
        assert s.active and s.eligible_jobs == ("j1", "j2")
        assert s.threads_max_items == 5
        assert s.threads_max_context_bytes == 4096
        assert s.recall_enabled and s.recall_max_items == 3

    def test_unknown_mode_is_shadow(self):
        assert ws.load_settings({"proactive": {"enabled": True, "mode": "yolo"}}).mode == "shadow"

    def test_load_without_config_reads_real_loader_and_never_raises(self):
        assert ws.load_settings().enabled is False

    def test_job_eligibility_needs_enabled_and_listing(self):
        on = ws.ProactiveSettings(enabled=True, eligible_jobs=("j1",))
        assert ws.job_eligible(on, "j1") is True
        assert ws.job_eligible(on, "j2") is False
        assert ws.job_eligible(ws.ProactiveSettings(eligible_jobs=("j1",)), "j1") is False


class TestDisabledIsInert:
    def test_every_entry_point_is_empty_and_creates_nothing(self, store):
        off = ws.ProactiveSettings()
        scope = sc()
        assert ws.record_outcomes(scope, [out("pkg", "failed", "d1")], off, now=NOW) == []
        assert ws.record_correction(scope, text="use /a", settings=off, now=NOW) is None
        assert ws.thread_context(scope, off, now=NOW) == ""
        assert ws.recall_context(scope, "publish", off, now=NOW) == ws.RecallResult("", ())
        assert ws.current_turn_note(scope, "publish", off, now=NOW) == ""
        assert ws.record_feedback(scope, "x", kind="reject", settings=off, now=NOW) is None
        assert ws.record_receipt(
            scope, "x", kind="proposed", proof=ws.PolicyProof("p"), settings=off, now=NOW
        ) is None
        assert not (store / "cron" / "notepad.db").exists()

    def test_enabled_but_no_data_reads_create_nothing(self, store):
        assert ws.thread_context(sc(), ON, now=NOW) == ""
        assert ws.recall_context(sc(), "anything", ON, now=NOW).text == ""
        assert not (store / "cron" / "notepad.db").exists()


# ── unfinished business / corrections / durability ────────────────────────


class TestUnfinishedBusiness:
    def test_success_plus_untested_deliverable_stays_open(self):
        scope = sc()
        rows = ws.record_outcomes(scope, [
            out("audit", "verified", "d-audit"),
            out("packaging", "not_run", "d-pack", summary="packaging was not run"),
        ], ON, now=NOW)
        assert [r["title"] for r in rows] == ["Unfinished: packaging"]
        open_rows = np.list_workstreams(scope, statuses=["open"])
        assert len(open_rows) == 1 and open_rows[0]["kind"] == "obligation"
        ev = np.list_evidence(scope, open_rows[0]["workstream_id"])
        assert [(e["source_kind"], e["source_id"], e["status"]) for e in ev] == [
            ("delegation", "d-pack", "unverified")
        ]

    def test_survives_new_session_and_restart_with_provenance(self):
        scope = sc()
        ws.record_outcomes(scope, [out("packaging", "failed", "d-pack",
                                       session_id="sess-1", summary="zip step failed")],
                           ON, now=NOW)
        # "new session after restart": same scope, nothing in memory.
        text = ws.thread_context(scope, ON, now=NOW + timedelta(days=2))
        assert "packaging" in text and "zip step failed" in text
        assert "[ws:" in text

    def test_unrelated_project_and_audience_see_nothing(self):
        ws.record_outcomes(sc(), [out("packaging", "failed", "d1")], ON, now=NOW)
        assert ws.thread_context(sc(project="proj-b"), ON, now=NOW) == ""
        assert ws.thread_context(sc(root="root-2"), ON, now=NOW) == ""
        assert ws.thread_context(sc(audience="group:c1"), ON, now=NOW) == ""

    def test_replay_is_idempotent_and_repeated_failure_adds_evidence_once(self):
        scope = sc()
        for _ in range(3):
            ws.record_outcomes(scope, [out("packaging", "failed", "d1")], ON, now=NOW)
        assert len(np.list_workstreams(scope)) == 1
        wid = np.list_workstreams(scope)[0]["workstream_id"]
        assert len(np.list_evidence(scope, wid)) == 1

    def test_only_same_owner_verified_evidence_resolves_an_obligation(self):
        scope = sc()
        ws.record_outcomes(scope, [out("packaging", "failed", "d1")], ON, now=NOW)
        ws.record_outcomes(scope, [out("packaging", "unverified", "d2")], ON, now=NOW)
        assert len(np.list_workstreams(scope, statuses=["open"])) == 2
        # Identical wording from another owner is not verification of either task.
        ws.record_outcomes(scope, [out("packaging", "verified", "d3")], ON, now=NOW)
        assert len(np.list_workstreams(scope, statuses=["open"])) == 2
        # A verified receipt from the exact original owner can resolve its item.
        ws.record_outcomes(scope, [out("packaging", "verified", "d1")], ON, now=NOW)
        assert len(np.list_workstreams(scope, statuses=["open"])) == 1
        resolved = np.list_workstreams(scope, statuses=["resolved"])
        assert len(resolved) == 2  # one unrelated verified progress item plus d1 obligation

    def test_verified_without_obligation_is_progress_not_open_work(self):
        scope = sc()
        ws.record_outcomes(scope, [out("audit", "verified", "d1")], ON, now=NOW)
        rows = np.list_workstreams(scope)
        assert [(r["kind"], r["status"]) for r in rows] == [("progress", "resolved")]
        assert ws.thread_context(scope, ON, now=NOW) == ""

    def test_non_private_scope_not_recorded_when_private_only(self):
        assert ws.record_outcomes(sc(audience="group:c1"),
                                  [out("p", "failed", "d1")], ON, now=NOW) == []

    def test_item_cap_comes_from_settings(self):
        small = ws.ProactiveSettings(enabled=True, threads_max_items=2)
        scope = sc()
        rows = ws.record_outcomes(
            scope, [out(f"d{i}", "failed", f"s{i}") for i in range(4)], small, now=NOW
        )
        assert len(rows) == 2
        assert len(np.list_workstreams(scope)) == 2


class TestCorrections:
    def test_correction_supersedes_and_keeps_identifiers_and_redacts_secrets(self):
        scope = sc()
        old = np.create_workstream(
            scope, kind="decision", title="path", summary="deploy from /srv/app/old",
            fingerprint="decision-path", now=NOW,
        )
        text = ("Use /srv/app/releases/v2.3.1 not /srv/app/old; "
                "tests/test_x.py::test_z failed, fix remains; "
                "key sk-abcdefghijklmnopqrstuvwxyz0123456789")
        new = ws.record_correction(
            scope, text=text, supersedes_id=old["workstream_id"],
            session_id="sess-9", settings=ON, now=NOW,
            origin=horigin(), human_reader=H.reader,
        )
        assert new["kind"] == "correction" and new["supersedes_id"] == old["workstream_id"]
        assert new["source"].startswith("human:")
        assert "/srv/app/releases/v2.3.1" in new["summary"]
        assert "tests/test_x.py::test_z" in new["summary"]
        assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in new["summary"]
        assert np.get_workstream(scope, old["workstream_id"])["status"] == "superseded"

    def test_stale_expected_revision_is_rejected(self):
        scope = sc()
        old = np.create_workstream(scope, kind="decision", title="t", summary="a",
                                   fingerprint="f", now=NOW)
        np.update_workstream(scope, old["workstream_id"], expected_revision=1,
                             summary="b", now=NOW)
        with pytest.raises(np.StaleRevision):
            ws.record_correction(scope, text="fix", supersedes_id=old["workstream_id"],
                                 expected_revision=1, settings=ON, now=NOW,
                                 origin=horigin(), human_reader=H.reader)

    def test_correction_and_failure_distinct_and_both_survive(self):
        scope = sc()
        ws.record_correction(scope, text="Use path /x, not /y", settings=ON, now=NOW,
                             origin=horigin(), human_reader=H.reader)
        ws.record_outcomes(scope, [out("tests", "failed", "d9",
                                       summary="test_z failed")], ON, now=NOW)
        text = ws.thread_context(scope, ON, now=NOW + timedelta(days=1))
        assert "Use path /x, not /y" in text
        assert "tests" in text and "test_z failed" in text


    def test_correction_text_without_an_owner_receipt_is_not_recorded(self):
        scope = sc()
        assert ws.record_correction(scope, text="Use path /x", settings=ON, now=NOW) is None
        assert ws.record_correction(scope, text="Use path /x", settings=ON, now=NOW,
                                    origin=ws.HumanOrigin(ws.current_profile(), "s", 1, "e", "human"),
                                    human_reader=H.reader) is None
        assert np.list_workstreams(scope) == []


# ── typed owner receipts ───────────────────────────────────────────────────


@pytest.fixture
def wid():
    scope = sc()
    ws.record_outcomes(scope, [out("packaging", "failed", "d0")], ON, now=NOW)
    return scope, np.list_workstreams(scope)[0]["workstream_id"]


def _execution(job="job-1", success=True, finish=True):
    import cron.executions as ex

    rec = ex.create_execution(job, source="test")
    ex.mark_execution_running(rec["id"])
    if finish:
        ex.finish_execution(rec["id"], success=success, error=None if success else "boom")
    return rec["id"]


def _delegation(delegation_id, *, complete=True, status="completed", deliver=False):
    import tools.async_delegation as ad

    ad._persist_dispatch({"delegation_id": delegation_id, "dispatched_at": time.time(),
                          "goal": "g", "session_key": "s"})
    if complete:
        ad._persist_completion(
            {"delegation_id": delegation_id, "status": status},
            {"status": status, "summary": "ok"},
        )
    if deliver:
        assert ad.mark_completion_delivered(delegation_id)


class TestTypedReceipts:
    def test_executed_requires_completed_execution_row(self, wid):
        scope, w = wid
        good = _execution()
        failed = _execution(success=False)
        running = _execution(finish=False)
        r = ws.record_receipt(scope, w, kind="executed",
                              proof=ws.ExecutedProof("job-1", good), settings=ON, now=NOW)
        assert r["kind"] == "executed" and r["owner"] == "execution" and r["source_id"] == good
        for bad in (failed, running, "no-such-id"):
            assert ws.record_receipt(scope, w, kind="executed",
                                     proof=ws.ExecutedProof("job-1", bad),
                                     settings=ON, now=NOW) is None
        assert ws.record_receipt(scope, w, kind="executed",
                                 proof=ws.ExecutedProof("other-job", good),
                                 settings=ON, now=NOW) is None
        assert len(np.list_receipts(scope, w, kinds=["executed"])) == 1

    def test_executed_from_clean_delegation_only(self, wid):
        scope, w = wid
        _delegation("del-ok")
        _delegation("del-bad", status="failed")
        _delegation("del-run", complete=False)
        assert ws.record_receipt(scope, w, kind="executed", proof=ws.DelegationProof("del-ok"),
                                 settings=ON, now=NOW)["owner"] == "execution"
        for d in ("del-bad", "del-run", "del-missing"):
            assert ws.record_receipt(scope, w, kind="executed", proof=ws.DelegationProof(d),
                                     settings=ON, now=NOW) is None

    def test_saved_requires_readback_hash(self, wid, tmp_path):
        scope, w = wid
        f = tmp_path / "draft.md"
        f.write_text("hello", encoding="utf-8")
        digest = hashlib.sha256(b"hello").hexdigest()
        r = ws.record_receipt(scope, w, kind="saved", proof=ws.SavedProof(str(f), digest),
                              settings=ON, now=NOW)
        assert r["owner"] == "persistence"
        assert ws.record_receipt(scope, w, kind="saved",
                                 proof=ws.SavedProof(str(f), "0" * 64),
                                 settings=ON, now=NOW) is None
        assert ws.record_receipt(scope, w, kind="saved",
                                 proof=ws.SavedProof(str(tmp_path / "nope"), digest),
                                 settings=ON, now=NOW) is None

    def test_delivered_requires_delivery_owner_row(self, wid):
        scope, w = wid
        _delegation("del-pending")
        _delegation("del-sent", deliver=True)
        assert ws.record_receipt(scope, w, kind="delivered",
                                 proof=ws.DeliveredProof("del-pending"),
                                 settings=ON, now=NOW) is None
        r = ws.record_receipt(scope, w, kind="delivered", proof=ws.DeliveredProof("del-sent"),
                              settings=ON, now=NOW)
        assert r["owner"] == "delivery"

    def test_executed_is_not_saved_or_delivered(self, wid):
        scope, w = wid
        eid = _execution()
        ws.record_receipt(scope, w, kind="executed", proof=ws.ExecutedProof("job-1", eid),
                          settings=ON, now=NOW)
        kinds = {r["kind"] for r in np.list_receipts(scope, w)}
        assert kinds == {"executed"}
        assert ws.outcome_label(np.list_receipts(scope, w)) == "draft ready"

    def test_wrong_proof_type_for_kind_writes_nothing(self, wid):
        scope, w = wid
        eid = _execution()
        for kind in ("saved", "delivered", "scheduled"):
            assert ws.record_receipt(scope, w, kind=kind,
                                     proof=ws.ExecutedProof("job-1", eid),
                                     settings=ON, now=NOW) is None
        assert ws.record_receipt(scope, w, kind="executed", proof="model says it ran",
                                 settings=ON, now=NOW) is None
        assert ws.record_receipt(scope, w, kind="executed", proof={"job": "x"},
                                 settings=ON, now=NOW) is None
        assert np.list_receipts(scope, w) == []

    def test_scheduled_requires_existing_occurrence(self, wid):
        scope, w = wid
        assert ws.record_receipt(scope, w, kind="scheduled",
                                 proof=ws.SchedulerProof("job-1", "h1"),
                                 settings=ON, now=NOW) is None
        np.ensure_occurrence(scope, w, occurrence_key="h1", now=NOW)
        r = ws.record_receipt(scope, w, kind="scheduled",
                              proof=ws.SchedulerProof("job-1", "h1"), settings=ON, now=NOW)
        assert r["owner"] == "scheduler"

    def test_policy_receipts_proposed_deferred_unknown(self, wid):
        scope, w = wid
        for kind in ("proposed", "deferred", "cancelled", "unknown"):
            r = ws.record_receipt(scope, w, kind=kind, proof=ws.PolicyProof(f"p-{kind}"),
                                  settings=ON, now=NOW)
            assert r["kind"] == kind and r["owner"] == "policy"

    def test_receipt_in_foreign_scope_is_rejected(self, wid):
        scope, w = wid
        eid = _execution()
        with pytest.raises(np.NotFound):
            ws.record_receipt(sc(project="other"), w, kind="executed",
                              proof=ws.ExecutedProof("job-1", eid), settings=ON, now=NOW)

    def test_outcome_labels_distinguish_the_five_user_states(self, wid):
        scope, w = wid
        assert ws.outcome_label([]) == "waiting"
        R = lambda *kinds: [{"kind": k} for k in kinds]  # noqa: E731
        assert ws.outcome_label(R("scheduled")) == "waiting"
        assert ws.outcome_label(R("executed")) == "draft ready"
        assert ws.outcome_label(R("executed", "saved")) == "saved locally, not delivered"
        assert ws.outcome_label(R("executed", "saved", "delivered")) == "delivered"
        assert ws.outcome_label(R("executed", "saved", "unknown")) == "delivery unknown"
        assert ws.outcome_label(R("cancelled")) == "failed"
        assert ws.outcome_label(R("executed", "delivered", "cancelled")) == "delivered"


# ── feedback / timing / novelty gates ──────────────────────────────────────


@pytest.fixture
def item():
    scope = sc()
    row = ws.record_outcomes(scope, [out("packaging", "failed", "d1")], ON, now=NOW)[0]
    return scope, row["workstream_id"]


def cand(wid_, text="packaging still failing, rerun the zip step?", **kw):
    return ws.Candidate(workstream_id=wid_, text=text, relevance=kw.pop("relevance", 0.9), **kw)


def decide(scope, c, settings=ON, **kw):
    kw.setdefault("user_timing_ok", True)
    kw.setdefault("now", NOW)
    return ws.evaluate_suggestion(c, scope, settings, **kw)


class TestFeedback:
    def test_only_explicit_user_feedback_is_recorded(self, item):
        scope, w = item
        assert ws.record_feedback(scope, w, kind="reject", actor="model", settings=ON, now=NOW) is None
        assert ws.record_feedback(scope, w, kind="silence", settings=ON, now=NOW) is None
        assert ws.feedback_state(scope, w) == "unknown"
        assert np.list_feedback(scope, w) == []

    def test_tty_user_claim_without_an_owner_receipt_writes_nothing(self, item):
        """The CLI/TTY and a caller's ``actor='user'`` are not human identity."""
        scope, w = item
        assert ws.record_feedback(scope, w, kind="reject", settings=ON, now=NOW) is None
        assert ws.record_feedback(scope, w, kind="reject", actor="user", settings=ON,
                                  origin=None, now=NOW) is None
        fake = ws.HumanOrigin(ws.current_profile(), "s", 1, "evt-x", "human", False)
        # default reader = the unpublished trusted API: fail closed
        assert ws.record_feedback(scope, w, kind="reject", settings=ON, origin=fake,
                                  now=NOW) is None
        assert np.list_feedback(scope, w) == []
        assert ws.approve_workstream(scope, w, settings=ON, now=NOW) is None
        assert np.get_workstream(scope, w)["approved"] is False

    def test_origin_binding_rejects_foreign_profile_synthetic_consumed_and_mismatch(self, item):
        scope, w = item
        from clover_state import SessionDB

        db = SessionDB()
        db.create_session("b-sess", "cli")
        db.append_message("b-sess", "user", "ok")
        good = H.issue(db, "b-sess")
        db.close()
        call = lambda o, **k: ws.record_feedback(  # noqa: E731
            scope, w, kind="reject", settings=ON, origin=o, human_reader=H.reader, now=NOW, **k)
        import dataclasses

        assert call(dataclasses.replace(good, profile="/some/other/home")) is None
        assert call(dataclasses.replace(good, origin="synthetic")) is None
        assert call(dataclasses.replace(good, consumed=True)) is None
        assert call(dataclasses.replace(good, message_row_id=good.message_row_id + 99)) is None
        assert call(dataclasses.replace(good, session_id="other")) is None
        assert call(dataclasses.replace(good, ingress_event_id="forged")) is None
        assert np.list_feedback(scope, w) == []
        assert call(good) is not None

    def test_same_ingress_event_is_idempotent(self, item):
        scope, w = item
        from clover_state import SessionDB

        db = SessionDB()
        db.create_session("i-sess", "cli")
        db.append_message("i-sess", "user", "not now")
        origin = H.issue(db, "i-sess")
        db.close()
        first = ws.record_feedback(scope, w, kind="defer", settings=ON, origin=origin,
                                   human_reader=H.reader, now=NOW)
        again = ws.record_feedback(scope, w, kind="defer", settings=ON, origin=origin,
                                   human_reader=H.reader, now=NOW + timedelta(minutes=5))
        assert again["feedback_id"] == first["feedback_id"]
        assert len(np.list_feedback(scope, w)) == 1

    def test_silence_is_unknown_and_never_rejection(self, item):
        scope, w = item
        d = decide(scope, cand(w), now=NOW + timedelta(days=30))
        assert d.action == "offer"
        assert ws.feedback_state(scope, w) == "unknown"

    def test_feedback_attributed_with_typed_receipt(self, item):
        scope, w = item
        row = hfb(scope, w, "accept")
        assert row["actor"] == "user" and row["kind"] == "accept"
        assert [r["kind"] for r in np.list_receipts(scope, w)] == ["feedback_accept"]
        assert ws.feedback_state(scope, w) == "accept"

    def test_latest_feedback_wins(self, item):
        scope, w = item
        hfb(scope, w, "reject")
        hfb(scope, w, "accept", now=NOW + timedelta(seconds=5))
        assert ws.feedback_state(scope, w) == "accept"


class TestSuggestionGate:
    def test_relevant_unsolicited_with_user_timing_is_offered_and_surfaced_when_active(self, item):
        scope, w = item
        d = decide(scope, cand(w))
        assert (d.action, d.surface) == ("offer", True)

    def test_shadow_mode_records_candidate_but_never_surfaces(self, item):
        scope, w = item
        shadow = ws.ProactiveSettings(enabled=True, mode="shadow", suggestions_enabled=True)
        d = decide(scope, cand(w), shadow)
        assert (d.action, d.reason, d.surface) == ("offer", "shadow", False)

    def test_disabled_suggestions_suppress(self, item):
        scope, w = item
        off = ws.ProactiveSettings(enabled=True, mode="active")
        assert decide(scope, cand(w), off).reason == "disabled"
        assert decide(scope, cand(w), ws.ProactiveSettings()).reason == "disabled"

    def test_irrelevant_suppressed(self, item):
        scope, w = item
        d = decide(scope, cand(w, relevance=0.1))
        assert (d.action, d.reason) == ("suppress", "irrelevant")

    def test_scope_mismatch_and_group_suppressed(self, item):
        scope, w = item
        assert decide(scope, cand(w), query_scope=sc(project="other")).reason == "scope"
        grp = sc(audience="group:c1")
        assert decide(grp, cand(w)).reason == "scope"

    def test_no_timing_preference_defers_unsolicited(self, item):
        scope, w = item
        d = decide(scope, cand(w), user_timing_ok=None)
        assert (d.action, d.reason) == ("defer", "no_timing_preference")
        assert decide(scope, cand(w, unsolicited=False), user_timing_ok=None).action == "offer"

    def test_wrong_time_defers(self, item):
        scope, w = item
        assert decide(scope, cand(w), user_timing_ok=False).reason == "timing"

    def test_not_now_blocks_repeat_during_cooldown_then_new_item_offered_later(self, item):
        scope, w = item
        hfb(scope, w, "defer", note="not now")
        during = NOW + timedelta(minutes=10)
        d = decide(scope, cand(w), now=during)
        assert (d.action, d.reason) == ("defer", "cooldown") and d.until
        new_row = ws.record_outcomes(scope, [out("docs", "failed", "d2")], ON, now=during)[0]
        assert decide(scope, cand(new_row["workstream_id"], text="docs build failed"),
                      now=during).action == "defer"
        after = NOW + timedelta(seconds=ON.suggestions_cooldown_seconds + 1)
        assert decide(scope, cand(new_row["workstream_id"], text="docs build failed"),
                      now=after).action == "offer"

    def test_explicit_defer_until_is_honoured(self, item):
        scope, w = item
        until = (NOW + timedelta(hours=5)).isoformat()
        hfb(scope, w, "defer", defer_until=until)
        assert decide(scope, cand(w), now=NOW + timedelta(hours=2)).action == "defer"
        # past scope cooldown (1h) but before the user-chosen time: still deferred
        assert decide(scope, cand(w), now=NOW + timedelta(hours=4)).reason == "cooldown"
        assert decide(scope, cand(w), now=NOW + timedelta(hours=6)).action == "offer"

    @pytest.mark.parametrize("kind,reason", [("reject", "rejected"), ("done", "done"), ("stop", "stopped")])
    def test_reject_done_stop_suppress_permanently(self, item, kind, reason):
        scope, w = item
        hfb(scope, w, kind)
        d = decide(scope, cand(w), now=NOW + timedelta(days=90))
        assert (d.action, d.reason) == ("suppress", reason)

    def test_duplicate_of_earlier_offer_suppressed_new_text_is_not(self, item):
        scope, w = item
        c = cand(w)
        ws.record_offer(scope, w, c, ON, session_id="s1", now=NOW)
        later = NOW + timedelta(days=2)
        assert decide(scope, c, now=later).reason == "duplicate"
        fresh = cand(w, text="packaging now needs a signing key rotation")
        assert decide(scope, fresh, now=later).action == "offer"

    def test_offer_cooldown_and_session_cap(self, item):
        scope, w = item
        c = cand(w)
        ws.record_offer(scope, w, c, ON, session_id="s1", now=NOW)
        other = cand(w, text="something else relevant to packaging")
        d = decide(scope, other, now=NOW + timedelta(minutes=5))
        assert (d.action, d.reason) == ("defer", "cooldown")
        d2 = decide(scope, other, now=NOW + timedelta(days=1), offered_this_session=1)
        assert (d2.action, d2.reason) == ("defer", "session_cap")

    def test_record_offer_writes_proposed_receipt_only_when_surfaced(self, item):
        scope, w = item
        shadow = ws.ProactiveSettings(enabled=True, mode="shadow", suggestions_enabled=True)
        assert ws.record_offer(scope, w, cand(w), shadow, session_id="s", now=NOW) is None
        r = ws.record_offer(scope, w, cand(w), ON, session_id="s", now=NOW)
        assert r["kind"] == "proposed" and r["owner"] == "policy"


class TestJudgeCannotAuthorize:
    def test_judge_only_suppresses_or_defers(self, item):
        scope, w = item
        offer = decide(scope, cand(w))
        assert ws.apply_judge(offer, None) == offer
        assert ws.apply_judge(offer, "ok") == offer
        assert ws.apply_judge(offer, "suppress").action == "suppress"
        assert ws.apply_judge(offer, "defer").action == "defer"
        assert ws.apply_judge(offer, "defer").surface is False

    @pytest.mark.parametrize("verdict", ["offer", "authorize", "approve", "ok", "yes", None, ""])
    def test_judge_cannot_promote(self, item, verdict):
        scope, w = item
        for base in (decide(scope, cand(w, relevance=0.0)),
                     decide(scope, cand(w), user_timing_ok=None)):
            out_ = ws.apply_judge(base, verdict)
            assert out_.action == base.action and out_.surface is False


class TestNaturalPrevalenceScoring:
    def test_categories_scored_separately_not_blended(self, item):
        scope, w = item
        c = cand(w)
        ws.record_offer(scope, w, c, ON, session_id="s", now=NOW - timedelta(days=3))
        fixtures = [
            ("useful_now", decide(scope, cand(w, text="rotate key", relevance=0.9))),
            ("useful_now", decide(scope, cand(w, text="rotate key", relevance=0.9))),
            ("wrong_time", decide(scope, cand(w, text="x y", relevance=0.9), user_timing_ok=False)),
            ("duplicate", decide(scope, c)),
            ("no_help", decide(scope, cand(w, relevance=0.0))),
            ("missed_need", decide(scope, cand(w, text="needed", relevance=0.0))),
        ]
        score = ws.score_gate(fixtures)
        assert score["useful_now"] == {"n": 2, "correct": 2}
        assert score["wrong_time"] == {"n": 1, "correct": 1}
        assert score["duplicate"] == {"n": 1, "correct": 1}
        assert score["no_help"] == {"n": 1, "correct": 1}
        assert score["missed_need"] == {"n": 1, "correct": 0}


# ── evidence sufficiency ───────────────────────────────────────────────────


def ev(key, status="verified", kind="cron_receipt", source_id="r1", event_at=None, **kw):
    return {"key": key, "status": status, "source_kind": kind, "source_id": source_id,
            "event_at": event_at if event_at is not None else NOW.isoformat(), **kw}


class TestEvidence:
    def test_classify(self):
        assert ws.classify_question("Did Windows tests pass?") == "historical"
        assert ws.classify_question("Is the CI build passing now?") == "live"
        assert ws.classify_question("what was decided about the path?") == "historical"

    def test_no_receipt_is_unknown_with_retrieval_plan_never_guess(self):
        q = "Did Windows tests pass?"
        plan = ws.plan_evidence(q, ["windows_tests"], [], now=NOW)
        assert plan.kind == "historical" and plan.missing == ("windows_tests",)
        assert "session_search" in plan.sources
        a = ws.assess_evidence(q, ["windows_tests"], [], now=NOW)
        assert a.status == "unknown" and a.cited == ()

    def test_anchored_verified_row_satisfies_historical(self):
        a = ws.assess_evidence("Did Windows tests pass?", ["windows_tests"],
                               [ev("windows_tests", source_id="exec-9")], now=NOW)
        assert a.status == "satisfied" and a.cited == ("cron_receipt:exec-9",)

    def test_unanchored_row_does_not_count(self):
        row = ev("windows_tests", source_id="")
        assert ws.assess_evidence("Did Windows tests pass?", ["windows_tests"], [row],
                                  now=NOW).status == "unknown"
        row = ev("windows_tests", event_at="")
        assert ws.assess_evidence("Did Windows tests pass?", ["windows_tests"], [row],
                                  now=NOW).status == "unknown"

    def test_partial_when_some_keys_missing(self):
        a = ws.assess_evidence("Did tests pass?", ["linux", "windows"],
                               [ev("linux")], now=NOW)
        assert a.status == "partial"
        assert ws.plan_evidence("Did tests pass?", ["linux", "windows"], [ev("linux")],
                                now=NOW).missing == ("windows",)

    def test_contradictory_never_satisfied(self):
        rows = [ev("w", source_id="a"), ev("w", status="failed", source_id="b")]
        assert ws.assess_evidence("Did tests pass?", ["w"], rows, now=NOW).status == "contradicted"

    def test_failed_evidence_is_failed_not_success(self):
        a = ws.assess_evidence("Did tests pass?", ["w"], [ev("w", status="failed")], now=NOW)
        assert a.status == "failed"

    def test_unverified_or_unknown_rows_are_unknown(self):
        for st in ("unverified", "unknown"):
            assert ws.assess_evidence("Did tests pass?", ["w"], [ev("w", status=st)],
                                      now=NOW).status == "unknown"

    def test_live_question_needs_fresh_live_source(self):
        q = "Is the build passing now?"
        old_receipt = ev("build", kind="cron_receipt")
        assert ws.assess_evidence(q, ["build"], [old_receipt], now=NOW).status == "unknown"
        stale = ev("build", kind="project_check",
                   event_at=(NOW - timedelta(hours=2)).isoformat())
        a = ws.assess_evidence(q, ["build"], [stale], now=NOW, live_ttl_seconds=300)
        assert a.status == "unknown" and "stale" in a.detail
        fresh = ev("build", kind="project_check", event_at=(NOW - timedelta(seconds=30)).isoformat())
        assert ws.assess_evidence(q, ["build"], [fresh], now=NOW).status == "satisfied"
        plan = ws.plan_evidence(q, ["build"], [old_receipt], now=NOW)
        assert plan.kind == "live" and "process_state" in plan.sources

    def test_ledger_rows_feed_assessment(self, item):
        scope, w = item
        np.add_evidence(scope, w, source_kind="cron_receipt", source_id="exec-1",
                        status="verified", key="windows_tests",
                        event_at=NOW.isoformat(), now=NOW)
        rows = np.list_evidence(scope, w)
        a = ws.assess_evidence("Did Windows tests pass?", ["windows_tests"], rows, now=NOW)
        assert a.status == "satisfied"


# ── bounded context selector ──────────────────────────────────────────────


def _constraint(scope, text, *, approved=True, kind="constraint", fp=None):
    return np.create_workstream(scope, kind=kind, title=text[:40], summary=text,
                                fingerprint=fp or text, approved=approved, now=NOW)


class TestRecall:
    def test_relevant_constraint_recalled_with_citation(self):
        scope = sc()
        row = _constraint(scope, "Never publish release artifacts for proj-a")
        r = ws.recall_context(scope, "can you publish the release now?", ON, now=NOW)
        assert [i["workstream_id"] for i in r.items] == [row["workstream_id"]]
        assert "Never publish release artifacts" in r.text
        assert f"[ws:{row['workstream_id']}@r1]" in r.text

    def test_unicode_text_recalled(self):
        scope = sc()
        _constraint(scope, "не публиковать релиз до проверки", fp="ru")
        r = ws.recall_context(scope, "можно ли публиковать релиз сегодня?", ON, now=NOW)
        assert len(r.items) == 1
        _constraint(scope, "不要发布正式版本之前先检查", fp="zh")
        r = ws.recall_context(scope, "可以发布正式版本吗", ON, now=NOW)
        assert any("不要发布" in i["summary"] for i in r.items)

    def test_irrelevant_unapproved_foreign_group_disabled_inject_nothing(self):
        scope = sc()
        _constraint(scope, "Never publish release artifacts for proj-a")
        _constraint(scope, "Always attach the changelog to publish release drafts",
                    approved=False, fp="unapproved")
        none = ws.RecallResult("", ())
        assert ws.recall_context(scope, "what's the weather tomorrow", ON, now=NOW) == none
        assert ws.recall_context(sc(project="proj-b"), "publish the release", ON, now=NOW) == none
        assert ws.recall_context(sc(root="r2"), "publish the release", ON, now=NOW) == none
        assert ws.recall_context(sc(audience="group:c1"), "publish the release", ON, now=NOW) == none
        off = ws.ProactiveSettings(enabled=True, recall_enabled=False)
        assert ws.recall_context(scope, "publish the release", off, now=NOW) == none
        # unapproved record never surfaces even though it matches
        r = ws.recall_context(scope, "publish release drafts changelog", ON, now=NOW)
        assert all(i["approved"] for i in r.items)

    def test_bounded_by_items_and_bytes(self):
        scope = sc()
        for i in range(6):
            _constraint(scope, f"publish release rule number {i} " + "x" * 400, fp=f"r{i}")
        s = ws.ProactiveSettings(enabled=True, recall_enabled=True, recall_max_items=2,
                                 recall_max_bytes=700)
        r = ws.recall_context(scope, "publish release rule", s, now=NOW)
        assert 1 <= len(r.items) <= 2
        assert len(r.text.encode("utf-8")) <= 700

    def test_terminal_records_not_recalled(self):
        scope = sc()
        row = _constraint(scope, "Never publish release artifacts for proj-a")
        np.update_workstream(scope, row["workstream_id"], expected_revision=1,
                             status="cancelled", now=NOW)
        assert ws.recall_context(scope, "publish the release", ON, now=NOW).text == ""


class TestThreadContext:
    def test_bounded_newest_first_and_cited(self):
        scope = sc()
        for i in range(10):
            ws.record_outcomes(scope, [out(f"deliverable-{i}", "failed", f"d{i}",
                                           summary="s" * 300)],
                               ON, now=NOW + timedelta(minutes=i))
        s = ws.ProactiveSettings(enabled=True, threads_max_context_bytes=900)
        text = ws.thread_context(scope, s, now=NOW + timedelta(hours=1))
        assert 0 < len(text.encode("utf-8")) <= 900
        assert text.index("deliverable-9") < text.index("deliverable-8")
        assert "[ws:" in text

    def test_disabled_and_nonprivate_empty(self):
        scope = sc()
        ws.record_outcomes(scope, [out("p", "failed", "d")], ON, now=NOW)
        assert ws.thread_context(scope, ws.ProactiveSettings(), now=NOW) == ""
        grp = sc(audience="group:c1")
        np.create_workstream(grp, kind="obligation", title="t", summary="s",
                             fingerprint="f", now=NOW)
        assert ws.thread_context(grp, ON, now=NOW) == ""

    def test_current_turn_note_joins_and_is_deterministic(self):
        scope = sc()
        ws.record_outcomes(scope, [out("packaging", "failed", "d1")], ON, now=NOW)
        _constraint(scope, "Never publish release artifacts for proj-a")
        a = ws.current_turn_note(scope, "should I publish the release?", ON, now=NOW)
        b = ws.current_turn_note(scope, "should I publish the release?", ON, now=NOW)
        assert a == b and "packaging" in a and "Never publish" in a
        assert ws.current_turn_note(scope, "hello", ON, now=NOW) == ws.thread_context(scope, ON, now=NOW)

    def test_approval_flag_set_through_cas_only(self):
        scope = sc()
        row = _constraint(scope, "Never publish release artifacts for proj-a", approved=False)
        assert ws.recall_context(scope, "publish the release", ON, now=NOW).text == ""
        np.update_workstream(scope, row["workstream_id"], expected_revision=1,
                             approved=True, now=NOW)
        assert ws.recall_context(scope, "publish the release", ON, now=NOW).text != ""


# ── work_lifecycle projection → ledger (producer → owner → consumer) ───────


class TestLifecycleProjection:
    def _receipts(self, *specs):
        from agent import foreground_time_limit as fl

        return [fl.DispatchReceipt(delegation_id=d, seq=i, goals=(g,))
                for i, (d, g) in enumerate(specs)]

    def test_durable_rows_become_typed_outcomes_then_open_obligations(self):
        import tools.async_delegation as ad
        from agent.work_lifecycle import unfinished_outcomes

        _delegation("l-ok", deliver=True)
        _delegation("l-unread")
        _delegation("l-bad", status="failed")
        _delegation("l-run", complete=False)
        _delegation("l-stop", status="cancelled")
        receipts = self._receipts(
            ("l-ok", "run the audit"), ("l-unread", "write the docs"),
            ("l-bad", "build the package"), ("l-run", "sign the artifact"),
            ("l-stop", "publish notes draft"),
        )
        outcomes = unfinished_outcomes(receipts)
        by_goal = {o.deliverable: o for o in outcomes}
        assert by_goal["run the audit"].status == "unverified"  # delivered, not independently checked
        assert by_goal["write the docs"].status == "unverified"   # result nobody consumed
        assert by_goal["build the package"].status == "failed"
        assert by_goal["sign the artifact"].status == "unverified"
        assert by_goal["publish notes draft"].status == "stopped"
        assert all(o.source_kind == "delegation" for o in outcomes)
        assert by_goal["build the package"].source_id == "l-bad"

        scope = sc()
        rows = ws.record_outcomes(scope, outcomes, ON, now=NOW)
        titles = sorted(r["title"] for r in rows)
        assert titles == [
            "Unfinished: build the package", "Unfinished: publish notes draft",
            "Unfinished: run the audit", "Unfinished: sign the artifact",
            "Unfinished: write the docs",
        ]
        # A delivered audit remains open until an independent check verifies it.
        text = ws.thread_context(scope, ON, now=NOW + timedelta(days=1))
        assert "build the package" in text and "run the audit" in text
        assert ad  # real ledger module used

    def test_missing_rows_are_pending_not_success(self):
        from agent.work_lifecycle import unfinished_outcomes

        out_ = unfinished_outcomes(self._receipts(("never-dispatched", "ghost task")))
        assert [(o.deliverable, o.status) for o in out_] == [("ghost task", "unverified")]

    def test_projection_never_raises_when_ledger_unavailable(self, monkeypatch):
        from agent import work_lifecycle as wl

        monkeypatch.setattr(wl, "slots_for",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
        assert wl.unfinished_outcomes(self._receipts(("x", "g"))) == []


class TestExpiry:
    def test_expired_record_is_neither_context_nor_recall_and_prune_marks_it(self):
        scope = sc()
        past = (NOW - timedelta(days=1)).isoformat()
        old = np.create_workstream(
            scope, kind="obligation", title="Unfinished: stale thing",
            summary="stale publish release thing", fingerprint="stale",
            approved=True, expires_at=past, now=NOW - timedelta(days=5))
        assert ws.thread_context(scope, ON, now=NOW) == ""
        assert ws.recall_context(scope, "publish release thing", ON, now=NOW).text == ""
        np.prune(scope, retention_days=30, now=NOW)
        assert np.get_workstream(scope, old["workstream_id"])["status"] == "expired"
        assert [h["op"] for h in np.list_history(scope, old["workstream_id"])] == ["create", "expire"]


class TestEndTurnRecording:
    """The finalizer's existing ``end_turn`` is the production producer."""

    def _agent(self, *specs, scope=None, completion=()):
        import types

        from agent import foreground_time_limit as fl

        own = {d: fl.DispatchReceipt(delegation_id=d, seq=i + 1, goals=(g,), turn_id="t1")
               for i, (d, g) in enumerate(specs)}
        a = types.SimpleNamespace(_fg_own_dispatches=own, _fg_dispatch_seq=len(own),
                                  _wl_floor=0, _wl_completion_ids=tuple(completion))
        if scope is not None:
            a._workstream_scope = scope
        return a

    def test_dispatch_turn_records_unfinished_helpers_for_the_envelope_scope(self, monkeypatch):
        from agent import work_lifecycle as wl

        _delegation("e-ok", deliver=True)
        _delegation("e-bad", status="failed")
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)
        scope = sc()
        wl.end_turn(self._agent(("e-ok", "run audit"), ("e-bad", "build package"), scope=scope))
        text = ws.thread_context(scope, ON, now=NOW)
        assert "build package" in text and "run audit" in text

    def test_no_envelope_or_disabled_records_nothing_and_touches_no_db(self, monkeypatch, store):
        from agent import work_lifecycle as wl

        _delegation("e-bad2", status="failed")
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)
        wl.end_turn(self._agent(("e-bad2", "build package")))
        assert not (store / "cron" / "notepad.db").exists()
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ws.ProactiveSettings())
        wl.end_turn(self._agent(("e-bad2", "build package"), scope=sc()))
        assert not (store / "cron" / "notepad.db").exists()

    def test_foreign_scope_object_and_ledger_errors_never_raise(self, monkeypatch):
        from agent import work_lifecycle as wl

        _delegation("e-bad3", status="failed")
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)
        a = self._agent(("e-bad3", "build package"))
        a._workstream_scope = "not-a-scope"
        wl.end_turn(a)
        a._workstream_scope = sc()
        monkeypatch.setattr(ws, "record_outcomes",
                            lambda *x, **k: (_ for _ in ()).throw(RuntimeError("locked")))
        wl.end_turn(a)

    def test_later_clean_consumed_completion_stays_open_until_independently_verified(self, monkeypatch):
        from agent import work_lifecycle as wl

        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)
        scope = sc()
        _delegation("r-1", complete=False)
        wl.end_turn(self._agent(("r-1", "sign artifact"), scope=scope))
        assert "sign artifact" in ws.thread_context(scope, ON, now=NOW)
        import tools.async_delegation as ad

        ad._persist_completion({"delegation_id": "r-1", "status": "completed"},
                               {"status": "completed", "summary": "ok"})
        assert ad.mark_completion_delivered("r-1")
        wl.end_turn(self._agent(("r-1", "sign artifact"), scope=scope))
        assert "sign artifact" in ws.thread_context(scope, ON, now=NOW)
        assert np.list_workstreams(scope, statuses=["open"])


class TestHostScopeDerivation:
    """One canonical Scope per host surface; end_turn needs no hand-set attribute."""

    def _agent(self, *specs, **attrs):
        import types

        from agent import foreground_time_limit as fl

        own = {d: fl.DispatchReceipt(delegation_id=d, seq=i + 1, goals=(g,), turn_id="t1")
               for i, (d, g) in enumerate(specs)}
        return types.SimpleNamespace(_fg_own_dispatches=own, _fg_dispatch_seq=len(own),
                                     _wl_floor=0, _wl_completion_ids=(), **attrs)

    @pytest.fixture
    def project(self, monkeypatch, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(proj))
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)
        return proj.resolve()

    def test_cli_turn_end_records_in_the_scope_the_context_seam_reads(self, project):
        from agent import work_lifecycle as wl

        _delegation("h-bad", status="failed")
        a = self._agent(("h-bad", "build package"), platform="cli")
        wl.end_turn(a)
        expect = ws.Scope(profile=ws.current_profile(), project=str(project),
                          audience="private", root="local")
        assert ws.scope_for_agent(a) == expect
        assert [r["title"] for r in np.list_workstreams(expect)] == ["Unfinished: build package"]
        # a fresh process/session derives the same scope and sees the open work
        fresh = self._agent(platform="cli")
        text = ws.current_turn_note(ws.scope_for_agent(fresh), "anything", ON)
        assert "build package" in text

    def test_direct_message_scope_uses_the_dm_identity(self, project):
        from agent import work_lifecycle as wl

        _delegation("h-dm", status="failed")
        a = self._agent(("h-dm", "send report"), platform="telegram",
                        _chat_type="dm", _user_id="u1", _chat_id="c9")
        wl.end_turn(a)
        scope = ws.scope_for_agent(a)
        assert (scope.audience, scope.root) == ("private:u1", "u1")
        assert len(np.list_workstreams(scope)) == 1

    @pytest.mark.parametrize("attrs", [
        dict(platform="telegram", _chat_type="group", _chat_id="g1", _user_id="u1"),
        dict(platform="telegram", _chat_type="dm"),        # DM without identity
        dict(platform="cron"),
        dict(),
    ])
    def test_shared_or_unidentified_surfaces_stay_inert(self, project, store, attrs):
        from agent import work_lifecycle as wl

        _delegation("h-grp", status="failed")
        wl.end_turn(self._agent(("h-grp", "x"), **attrs))
        assert not (store / "cron" / "notepad.db").exists()

    def test_explicit_host_envelope_wins_and_foreign_object_is_inert(self, project, store):
        from agent import work_lifecycle as wl

        _delegation("h-env", status="failed")
        env = sc(project="envelope-proj")
        wl.end_turn(self._agent(("h-env", "x"), platform="cli", _workstream_scope=env))
        assert len(np.list_workstreams(env)) == 1
        other = ws.Scope(ws.current_profile(), str(project), "private", "local")
        assert np.list_workstreams(other) == []
        wl.end_turn(self._agent(("h-env", "x"), platform="cli", _workstream_scope=object()))
        assert np.list_workstreams(other) == []

    def test_disabled_is_byte_inert(self, project, store, monkeypatch):
        from agent import work_lifecycle as wl

        monkeypatch.setattr(ws, "load_settings", lambda config=None: ws.ProactiveSettings())
        _delegation("h-off", status="failed")
        wl.end_turn(self._agent(("h-off", "x"), platform="cli"))
        assert not (store / "cron" / "notepad.db").exists()


class TestExplicitCorrection:
    """Persisted only from an owner-confirmed, current, user-role row of the
    real SessionDB carrying the explicit marker — never inferred or claimed."""

    TEXT = "Correction: use /srv/app/releases/v2.3.1 not /srv/app/old; tests/test_x.py::test_z failed, fix remains"

    @pytest.fixture
    def sdb(self):
        from clover_state import SessionDB

        db = SessionDB()
        db.create_session("sess-1", "cli")
        yield db
        db.close()

    def test_marker_is_anchored_and_explicit(self):
        f = ws.explicit_correction_from_message
        assert f("/correct use /a not /b") == "use /a not /b"
        assert f("  /correction: use /a not /b") == "use /a not /b"
        assert f("Correction: use /a\nnot /b") == "use /a\nnot /b"
        assert f("correction : use /a") == "use /a"
        for no in ("I think the correction: use /a", "that's not right, use /a", "",
                   "Correction:", "/correct   ", "please /correct use /a", None, 7):
            assert f(no) is None

    def test_target_id_is_parsed_only_when_explicit(self):
        p = ws.parse_explicit_correction
        assert p("/correct ws_0123456789abcdef: use /a") == ("ws_0123456789abcdef", "use /a")
        assert p("/correct use ws_0123456789abcdef as base") == (None, "use ws_0123456789abcdef as base")

    def test_current_user_row_is_recorded_and_survives_a_fresh_sessiondb(self, sdb, tmp_path):
        from clover_state import SessionDB

        sdb.append_message("sess-1", "user", self.TEXT)
        sdb.append_message("sess-1", "assistant", "Noted.")
        scope = sc()
        origin = H.issue(sdb, "sess-1")
        row = ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW,
                                         human_reader=H.reader)
        assert row["kind"] == "correction" and row["approved"] is True
        assert row["source"] == f"human:{origin.ingress_event_id}"
        assert row["session_id"] == "sess-1"
        fresh = SessionDB(tmp_path / "fresh.db")
        fresh.create_session("sess-2", "cli")
        assert fresh.get_messages("sess-2") == []
        note = ws.current_turn_note(scope, "which release path?", ON, now=NOW + timedelta(days=3))
        assert "/srv/app/releases/v2.3.1" in note and "tests/test_x.py::test_z" in note
        fresh.close()

    def test_synthetic_role_user_row_with_marker_is_never_a_correction(self, sdb):
        """PRESERVED PROBE: a database role of 'user' is not human origin."""
        scope = sc()
        sdb.append_message("sess-1", "user", self.TEXT)        # no owner receipt issued
        assert ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW) is None
        assert ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW,
                                          human_reader=H.reader) is None
        assert np.list_workstreams(scope) == []

    def test_receipt_must_be_human_unconsumed_bound_to_this_profile_session_and_row(self, sdb):
        import dataclasses

        scope = sc()
        sdb.append_message("sess-1", "user", self.TEXT)
        good = H.issue(sdb, "sess-1")
        key = ("sess-1", good.message_row_id)
        for bad in (dataclasses.replace(good, origin="synthetic"),
                    dataclasses.replace(good, origin="assistant"),
                    dataclasses.replace(good, consumed=True),
                    dataclasses.replace(good, profile="/other/profile"),
                    dataclasses.replace(good, session_id="sess-9"),
                    dataclasses.replace(good, message_row_id=good.message_row_id + 1)):
            H.rows[key] = bad
            assert ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW,
                                              human_reader=H.reader) is None
        assert np.list_workstreams(scope) == []
        H.rows[key] = good
        assert ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW,
                                          human_reader=H.reader) is not None

    def test_assistant_tool_stale_and_unmarked_rows_are_not_corrections(self, sdb):
        scope = sc()
        sdb.append_message("sess-1", "assistant", self.TEXT)          # model prose, same words
        assert ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW,
                                          human_reader=H.reader) is None
        sdb.append_message("sess-1", "user", self.TEXT)
        H.issue(sdb, "sess-1")
        sdb.append_message("sess-1", "tool", self.TEXT, tool_call_id="c1")
        assert ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW,
                                          human_reader=H.reader) is not None
        # a newer user row (even a receipted, ordinary one) makes the marker row stale
        other = sc(project="p2")
        sdb.append_message("sess-1", "user", "thanks, now run the audit")
        H.issue(sdb, "sess-1")
        assert ws.capture_user_correction(other, session_id="sess-1", settings=ON, now=NOW,
                                          human_reader=H.reader) is None
        assert np.list_workstreams(other) == []

    def test_unknown_session_and_untrusted_inputs_record_nothing(self, store):
        scope = sc()
        f = ws.capture_user_correction
        assert f(scope, session_id="never-created", settings=ON, now=NOW, human_reader=H.reader) is None
        assert f(scope, session_id="", settings=ON, now=NOW, human_reader=H.reader) is None
        assert f(scope, session_id="sess-1", settings=ws.ProactiveSettings(), now=NOW,
                 human_reader=H.reader) is None
        assert f(sc(audience="group:c1"), session_id="sess-1", settings=ON, now=NOW,
                 human_reader=H.reader) is None
        assert np.list_workstreams(scope) == []

    def test_supersedes_named_record_with_history_and_redacts(self, sdb):
        scope = sc()
        old = np.create_workstream(scope, kind="decision", title="p", summary="deploy /srv/old",
                                   fingerprint="d", now=NOW)
        sdb.append_message(
            "sess-1", "user",
            f"/correct {old['workstream_id']}: deploy /srv/new token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        H.issue(sdb, "sess-1")
        row = ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW,
                                         human_reader=H.reader)
        assert row["supersedes_id"] == old["workstream_id"]
        assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in row["summary"]
        assert "/srv/new" in row["summary"]
        assert [h["op"] for h in np.list_history(scope, old["workstream_id"])] == ["create", "supersede"]

    def test_unknown_named_target_is_not_recorded(self, sdb):
        scope = sc()
        sdb.append_message("sess-1", "user", "/correct ws_0000000000000000: fix it")
        H.issue(sdb, "sess-1")
        with pytest.raises(np.NotFound):
            ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW,
                                       human_reader=H.reader)
        assert np.list_workstreams(scope) == []

    def test_replay_adds_one_record(self, sdb):
        scope = sc()
        sdb.append_message("sess-1", "user", self.TEXT)
        H.issue(sdb, "sess-1")
        for _ in range(3):
            ws.capture_user_correction(scope, session_id="sess-1", settings=ON, now=NOW,
                                       human_reader=H.reader)
        assert len(np.list_workstreams(scope, kinds=["correction"])) == 1

    def _agent(self, sdb, ids=()):
        import types

        return types.SimpleNamespace(platform="cli", session_id="sess-1", _session_db=sdb,
                                     _fg_own_dispatches={}, _fg_dispatch_seq=0, _wl_floor=0,
                                     _wl_completion_ids=tuple(ids))

    def test_end_turn_records_nothing_until_the_trusted_api_exists(self, sdb, monkeypatch, tmp_path):
        """PRESERVED PROBE: the production ingress fails closed by default."""
        from agent import work_lifecycle as wl

        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(proj))
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)
        sdb.append_message("sess-1", "user", self.TEXT)
        H.issue(sdb, "sess-1")                                  # receipt exists in the double only
        agent = self._agent(sdb)
        wl.end_turn(agent)
        assert np.list_workstreams(ws.scope_for_agent(agent)) == []

    def test_end_turn_consumes_the_owner_receipt_once_published(self, sdb, monkeypatch, tmp_path):
        from agent import work_lifecycle as wl

        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(proj))
        monkeypatch.setattr(ws, "load_settings", lambda config=None: ON)
        monkeypatch.setattr(ws, "_HUMAN_ORIGIN_READER", H.reader)
        sdb.append_message("sess-1", "user", self.TEXT)
        origin = H.issue(sdb, "sess-1")
        agent = self._agent(sdb)
        wl.end_turn(agent)
        wl.end_turn(agent)                                      # replay: idempotent
        scope = ws.scope_for_agent(agent)
        rows = np.list_workstreams(scope)
        assert [r["kind"] for r in rows] == ["correction"]
        assert rows[0]["source"] == f"human:{origin.ingress_event_id}"
        # a helper-completion turn's synthetic user row is never a correction
        other = self._agent(sdb, ids=("d-1",))
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        wl.end_turn(other)
        assert np.list_workstreams(ws.scope_for_agent(other)) == []

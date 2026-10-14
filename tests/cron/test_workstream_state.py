"""Workstream ledger state in the existing notepad DB (PLAN-v1, state owner).

Real SQLite in a temporary CLOVER_HOME; no mocks of the storage owner.
Covers: per-transaction profile path + NOTEPAD_FILE override, legacy KV
byte-compat, disabled = no workstream tables, idempotent/fail-closed
migration, caps, CAS, supersession, isolation, retention, durable
occurrence recovery, runtime-only authority link and atomic budgets.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from cron import notepad as np


NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = tmp_path / "cron" / "notepad.db"
    monkeypatch.setattr(np, "NOTEPAD_FILE", path)
    return path


def scope(project="proj-a", audience="private", root="root-1", profile="/home/p1"):
    from agent.workstreams import Scope

    return Scope(profile=profile, project=project, audience=audience, root=root)


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def make(sc, text="ship packaging", fp=None, **kw):
    return np.create_workstream(
        sc,
        kind=kw.pop("kind", "obligation"),
        title=kw.pop("title", "t"),
        summary=text,
        fingerprint=fp or text,
        now=kw.pop("now", NOW),
        **kw,
    )


# ── profile path / legacy KV / disabled ────────────────────────────────────


class TestPathAndLegacy:
    def test_active_profile_resolved_per_transaction(self, monkeypatch, tmp_path):
        monkeypatch.setattr(np, "NOTEPAD_FILE", None)
        home_a, home_b = tmp_path / "a", tmp_path / "b"
        monkeypatch.setenv("CLOVER_HOME", str(home_a))
        np.set_note("job", "k", "from-a")
        monkeypatch.setenv("CLOVER_HOME", str(home_b))
        assert np.get_note("job", "k") is None
        np.set_note("job", "k", "from-b")
        assert (home_a / "cron" / "notepad.db").exists()
        assert (home_b / "cron" / "notepad.db").exists()
        monkeypatch.setenv("CLOVER_HOME", str(home_a))
        assert np.get_note("job", "k") == "from-a"

    def test_notepad_file_override_wins(self, db):
        np.set_note("job", "k", "v")
        assert db.exists()
        assert np.get_note("job", "k") == "v"

    def test_legacy_kv_does_not_create_workstream_tables(self, db):
        np.set_note("job", "k", "v")
        np.list_notes("job")
        np.render_notepad_section("job")
        assert tables(db) == {"cron_notepad"}
        assert np.workstream_schema_version() == 0

    def test_workstream_reads_never_create_db_or_tables(self, db):
        sc = scope()
        assert np.list_workstreams(sc) == []
        assert np.get_workstream(sc, "nope") is None
        assert np.pending_occurrences(sc) == []
        assert not db.exists()
        np.set_note("job", "k", "v")
        assert np.list_workstreams(sc) == []
        assert tables(db) == {"cron_notepad"}

    def test_legacy_rendering_bytes_unchanged_after_workstream_use(self, db):
        np.set_note("job", "k", "v")
        before = np.render_notepad_section("job")
        make(scope())
        assert np.render_notepad_section("job") == before
        assert "workstream" not in before.lower()


# ── migration ──────────────────────────────────────────────────────────────


class TestMigration:
    def test_first_write_migrates_to_v1_idempotently(self, db):
        make(scope())
        assert np.workstream_schema_version() == 1
        make(scope(), text="another", fp="another")
        assert np.workstream_schema_version() == 1
        names = tables(db)
        for t in (
            "workstreams", "work_evidence", "work_receipts", "work_feedback",
            "work_history", "work_occurrences", "work_authority",
            "workstream_meta",
        ):
            assert t in names

    def test_newer_schema_fails_closed_and_preserves_rows(self, db):
        sc = scope()
        make(sc)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE workstream_meta SET value='99' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with pytest.raises(np.WorkstreamSchemaError):
            make(sc, text="blocked", fp="blocked")
        with pytest.raises(np.WorkstreamSchemaError):
            np.list_workstreams(sc)
        conn = sqlite3.connect(db)
        n = conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0]
        conn.close()
        assert n == 1

    def test_older_binary_ignores_tables_legacy_kv_still_works(self, db):
        make(scope())
        np.set_note("job", "k", "v")
        assert np.get_note("job", "k") == "v"


# ── records, caps, CAS ─────────────────────────────────────────────────────


class TestRecords:
    def test_create_get_list_roundtrip(self, db):
        sc = scope()
        row = make(sc, text="Use path /srv/a, not /srv/b", kind="correction")
        got = np.get_workstream(sc, row["workstream_id"])
        assert got["summary"] == "Use path /srv/a, not /srv/b"
        assert got["status"] == "open"
        assert got["revision"] == 1
        assert [r["workstream_id"] for r in np.list_workstreams(sc)] == [
            row["workstream_id"]
        ]

    def test_create_is_idempotent_by_fingerprint(self, db):
        sc = scope()
        a = make(sc, fp="same")
        b = make(sc, text="different words", fp="same")
        assert a["workstream_id"] == b["workstream_id"]
        assert len(np.list_workstreams(sc)) == 1

    def test_item_cap_enforced_and_store_untouched(self, db):
        sc = scope()
        for i in range(3):
            make(sc, text=f"item {i}", max_items=3)
        with pytest.raises(np.WorkstreamCapError):
            make(sc, text="item 3", max_items=3)
        assert len(np.list_workstreams(sc)) == 3

    def test_terminal_items_do_not_count_against_cap(self, db):
        sc = scope()
        rows = [make(sc, text=f"i{i}", max_items=2) for i in range(2)]
        np.update_workstream(
            sc, rows[0]["workstream_id"], expected_revision=1, status="resolved", now=NOW
        )
        make(sc, text="i2", max_items=2)

    def test_aggregate_byte_cap(self, db):
        sc = scope()
        make(sc, text="x" * 600, fp="a", max_bytes=1000)
        with pytest.raises(np.WorkstreamCapError):
            make(sc, text="y" * 600, fp="b", max_bytes=1000)

    def test_summary_over_value_cap_rejected(self, db):
        with pytest.raises(ValueError):
            make(scope(), text="z" * (np.MAX_WORKSTREAM_SUMMARY_BYTES + 1))

    def test_cas_stale_revision_rejected(self, db):
        sc = scope()
        row = make(sc)
        wid = row["workstream_id"]
        upd = np.update_workstream(sc, wid, expected_revision=1, summary="v2", now=NOW)
        assert upd["revision"] == 2
        with pytest.raises(np.StaleRevision):
            np.update_workstream(sc, wid, expected_revision=1, summary="lost", now=NOW)
        assert np.get_workstream(sc, wid)["summary"] == "v2"

    def test_cas_race_exactly_one_winner(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        results = []
        barrier = threading.Barrier(6)

        def worker(i):
            barrier.wait()
            try:
                np.update_workstream(
                    sc, wid, expected_revision=1, summary=f"w{i}", now=NOW
                )
                results.append("ok")
            except np.StaleRevision:
                results.append("stale")

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert results.count("ok") == 1
        assert results.count("stale") == 5
        assert np.get_workstream(sc, wid)["revision"] == 2

    def test_supersession_keeps_provenance_and_history(self, db):
        sc = scope()
        old = make(sc, text="path is /y", kind="decision")
        new = np.supersede_workstream(
            sc, old["workstream_id"], expected_revision=1, kind="correction",
            title="fix", summary="path is /x not /y", fingerprint="corr-1", now=NOW,
        )
        assert new["supersedes_id"] == old["workstream_id"]
        assert np.get_workstream(sc, old["workstream_id"])["status"] == "superseded"
        ops = [h["op"] for h in np.list_history(sc, old["workstream_id"])]
        assert ops == ["create", "supersede"]

    def test_supersede_stale_revision_changes_nothing(self, db):
        sc = scope()
        old = make(sc)
        np.update_workstream(sc, old["workstream_id"], expected_revision=1, summary="b", now=NOW)
        with pytest.raises(np.StaleRevision):
            np.supersede_workstream(
                sc, old["workstream_id"], expected_revision=1, kind="correction",
                title="t", summary="c", fingerprint="c", now=NOW,
            )
        assert len(np.list_workstreams(sc)) == 1


# ── isolation ──────────────────────────────────────────────────────────────


class TestIsolation:
    @pytest.mark.parametrize(
        "other",
        [
            dict(project="proj-b"),
            dict(audience="group:chan-1"),
            dict(root="root-2"),
            dict(profile="/home/p2"),
        ],
    )
    def test_each_axis_isolates(self, db, other):
        sc = scope()
        row = make(sc)
        foreign = scope(**other)
        assert np.list_workstreams(foreign) == []
        assert np.get_workstream(foreign, row["workstream_id"]) is None
        with pytest.raises(np.NotFound):
            np.update_workstream(
                foreign, row["workstream_id"], expected_revision=1, summary="x", now=NOW
            )
        assert np.get_workstream(sc, row["workstream_id"])["summary"] == "ship packaging"

    def test_same_fingerprint_in_two_scopes_are_distinct_rows(self, db):
        a = make(scope(project="a"), fp="f")
        b = make(scope(project="b"), fp="f")
        assert a["workstream_id"] != b["workstream_id"]

    def test_evidence_receipts_feedback_follow_scope(self, db):
        sc, foreign = scope(), scope(project="other")
        wid = make(sc)["workstream_id"]
        np.add_evidence(sc, wid, source_kind="delegation", source_id="d1",
                        status="failed", summary="s", now=NOW)
        np.add_receipt(sc, wid, kind="executed", owner="execution", source_id="e1", now=NOW)
        np.add_feedback(sc, wid, kind="reject", actor="user", now=NOW)
        assert len(np.list_evidence(sc, wid)) == 1
        assert np.list_evidence(foreign, wid) == []
        assert np.list_receipts(foreign) == []
        assert np.list_feedback(foreign, wid) == []
        with pytest.raises(np.NotFound):
            np.add_evidence(foreign, wid, source_kind="delegation", source_id="d2",
                            status="failed", now=NOW)


# ── evidence / receipts / feedback rows ────────────────────────────────────


class TestEvidenceAndReceipts:
    def test_evidence_unique_per_source(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        a = np.add_evidence(sc, wid, source_kind="delegation", source_id="d1",
                            status="failed", now=NOW)
        b = np.add_evidence(sc, wid, source_kind="delegation", source_id="d1",
                            status="failed", now=NOW)
        assert a["evidence_id"] == b["evidence_id"]
        assert len(np.list_evidence(sc, wid)) == 1

    def test_evidence_rejects_unknown_status(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        with pytest.raises(ValueError):
            np.add_evidence(sc, wid, source_kind="x", source_id="1", status="great", now=NOW)

    def test_receipt_kinds_are_distinct_events(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        for kind in ("executed", "saved", "delivered"):
            np.add_receipt(sc, wid, kind=kind, owner="o", source_id="same", now=NOW)
        assert {r["kind"] for r in np.list_receipts(sc, wid)} == {
            "executed", "saved", "delivered",
        }

    def test_receipt_replay_returns_existing(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        a = np.add_receipt(sc, wid, kind="executed", owner="o", source_id="e", now=NOW)
        b = np.add_receipt(sc, wid, kind="executed", owner="o", source_id="e", now=NOW)
        assert a["receipt_id"] == b["receipt_id"]

    def test_receipt_rejects_unknown_kind(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        with pytest.raises(ValueError):
            np.add_receipt(sc, wid, kind="vibes", owner="o", source_id="e", now=NOW)

    def test_feedback_kinds_validated(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        with pytest.raises(ValueError):
            np.add_feedback(sc, wid, kind="silence", actor="user", now=NOW)


# ── retention ──────────────────────────────────────────────────────────────


class TestRetention:
    def test_prune_removes_only_old_terminal_rows(self, db):
        sc = scope()
        old_done = make(sc, text="old done", now=NOW - timedelta(days=40))
        np.update_workstream(
            sc, old_done["workstream_id"], expected_revision=1, status="resolved",
            now=NOW - timedelta(days=39),
        )
        old_open = make(sc, text="old open", now=NOW - timedelta(days=40))
        fresh_done = make(sc, text="fresh done", now=NOW - timedelta(days=1))
        np.update_workstream(
            sc, fresh_done["workstream_id"], expected_revision=1, status="resolved",
            now=NOW - timedelta(days=1),
        )
        np.add_evidence(sc, old_done["workstream_id"], source_kind="k", source_id="1",
                        status="verified", now=NOW)
        removed = np.prune(sc, retention_days=30, now=NOW)
        assert removed == 1
        ids = {r["workstream_id"] for r in np.list_workstreams(sc, limit=50)}
        assert old_open["workstream_id"] in ids
        assert fresh_done["workstream_id"] in ids
        assert old_done["workstream_id"] not in ids
        assert np.list_evidence(sc, old_done["workstream_id"]) == []

    def test_prune_without_tables_is_noop(self, db):
        assert np.prune(scope(), retention_days=30, now=NOW) == 0
        assert not db.exists()


# ── durable occurrences (monitor → pending → claim) ────────────────────────


class TestOccurrences:
    def test_pending_survives_and_claims_exactly_once(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        np.ensure_occurrence(sc, wid, occurrence_key="hash-1", now=NOW)
        again = np.ensure_occurrence(sc, wid, occurrence_key="hash-1", now=NOW)
        assert again["state"] == "pending"
        assert len(np.pending_occurrences(sc)) == 1
        first = np.claim_occurrence(sc, wid, occurrence_key="hash-1", claimer="p1", now=NOW)
        second = np.claim_occurrence(sc, wid, occurrence_key="hash-1", claimer="p2", now=NOW)
        assert first["state"] == "claimed" and first["claimer"] == "p1"
        assert second is None
        assert np.pending_occurrences(sc) == []

    def test_concurrent_claim_single_winner(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        np.ensure_occurrence(sc, wid, occurrence_key="h", now=NOW)
        wins = []
        barrier = threading.Barrier(6)

        def worker(i):
            barrier.wait()
            got = np.claim_occurrence(sc, wid, occurrence_key="h", claimer=f"c{i}", now=NOW)
            if got is not None:
                wins.append(i)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(wins) == 1

    def test_claim_requires_existing_occurrence(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        assert np.claim_occurrence(sc, wid, occurrence_key="never", claimer="c", now=NOW) is None

    def test_settle_requires_claimer_and_is_terminal(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        np.ensure_occurrence(sc, wid, occurrence_key="h", now=NOW)
        np.claim_occurrence(sc, wid, occurrence_key="h", claimer="c1", now=NOW)
        with pytest.raises(np.StaleRevision):
            np.settle_occurrence(sc, wid, occurrence_key="h", claimer="other",
                                 state="executed", now=NOW)
        done = np.settle_occurrence(sc, wid, occurrence_key="h", claimer="c1",
                                    state="executed", now=NOW)
        assert done["state"] == "executed"
        with pytest.raises(np.StaleRevision):
            np.settle_occurrence(sc, wid, occurrence_key="h", claimer="c1",
                                 state="unknown", now=NOW)

    def test_settle_rejects_non_terminal_target(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        np.ensure_occurrence(sc, wid, occurrence_key="h", now=NOW)
        np.claim_occurrence(sc, wid, occurrence_key="h", claimer="c1", now=NOW)
        with pytest.raises(ValueError):
            np.settle_occurrence(sc, wid, occurrence_key="h", claimer="c1",
                                 state="pending", now=NOW)

    def test_crash_after_claim_becomes_unknown_never_pending(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        np.ensure_occurrence(sc, wid, occurrence_key="dead", now=NOW)
        np.claim_occurrence(sc, wid, occurrence_key="dead", claimer="gone", now=NOW)
        np.ensure_occurrence(sc, wid, occurrence_key="waiting", now=NOW)
        np.ensure_occurrence(sc, wid, occurrence_key="live", now=NOW)
        np.claim_occurrence(sc, wid, occurrence_key="live", claimer="alive", now=NOW)
        changed = np.recover_occurrences(sc, owner_alive=lambda c: c == "alive", now=NOW)
        assert [(r["occurrence_key"], r["state"]) for r in changed] == [("dead", "unknown")]
        # detection-to-claim crash: never claimed stays pending and is claimable
        assert [r["occurrence_key"] for r in np.pending_occurrences(sc)] == ["waiting"]
        assert np.claim_occurrence(sc, wid, occurrence_key="dead", claimer="n", now=NOW) is None
        got = np.claim_occurrence(sc, wid, occurrence_key="waiting", claimer="n", now=NOW)
        assert got["state"] == "claimed"

    def test_occurrence_isolated_by_scope(self, db):
        sc, foreign = scope(), scope(root="other")
        wid = make(sc)["workstream_id"]
        np.ensure_occurrence(sc, wid, occurrence_key="h", now=NOW)
        assert np.pending_occurrences(foreign) == []
        assert np.claim_occurrence(foreign, wid, occurrence_key="h", claimer="c", now=NOW) is None


# ── authority link + budget ────────────────────────────────────────────────


class TestAuthority:
    PLAN = {"delegation_id": "d1", "request_id": "r1", "generation": 3,
            "receipt_id": "rc1", "stage_digest": "abc"}

    def test_link_and_get_roundtrip_verbatim(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        assert np.get_authority(sc, wid) is None
        np.link_authority(sc, wid, plan_ref=self.PLAN,
                          budget={"turns": 3, "tokens": 1000, "seconds": 60}, now=NOW)
        got = np.get_authority(sc, wid)
        assert got["plan_ref"] == self.PLAN
        assert got["budget"] == {"turns": 3, "tokens": 1000, "seconds": 60}

    def test_summary_update_cannot_touch_authority(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        np.link_authority(sc, wid, plan_ref=self.PLAN, budget={"turns": 1}, now=NOW)
        np.update_workstream(sc, wid, expected_revision=1,
                             summary='{"delegation_id": "evil"}', now=NOW)
        assert np.get_authority(sc, wid)["plan_ref"] == self.PLAN

    def test_authority_is_scope_isolated(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        np.link_authority(sc, wid, plan_ref=self.PLAN, now=NOW)
        assert np.get_authority(scope(project="x"), wid) is None
        with pytest.raises(np.NotFound):
            np.link_authority(scope(project="x"), wid, plan_ref=self.PLAN, now=NOW)

    def test_budget_spend_atomic_and_exhaustion_spends_nothing(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        np.link_authority(sc, wid, plan_ref=self.PLAN,
                          budget={"turns": 2, "tokens": 100, "seconds": 30}, now=NOW)
        left = np.spend_budget(sc, wid, turns=1, tokens=40, seconds=10)
        assert left == {"turns": 1, "tokens": 60, "seconds": 20}
        with pytest.raises(np.BudgetExhausted):
            np.spend_budget(sc, wid, turns=1, tokens=61, seconds=0)
        assert np.get_authority(sc, wid)["budget"] == {"turns": 1, "tokens": 60, "seconds": 20}

    def test_budget_race_cannot_overspend(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        np.link_authority(sc, wid, plan_ref=self.PLAN, budget={"turns": 3}, now=NOW)
        ok = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                np.spend_budget(sc, wid, turns=1)
                ok.append(1)
            except np.BudgetExhausted:
                pass

        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(ok) == 3
        assert np.get_authority(sc, wid)["budget"]["turns"] == 0

    def test_spend_without_link_is_budget_exhausted(self, db):
        sc = scope()
        wid = make(sc)["workstream_id"]
        with pytest.raises(np.BudgetExhausted):
            np.spend_budget(sc, wid, turns=1)


# ── cross-process: the DB, not a process-local lock, is the arbiter ────────


_CHILD = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from cron import notepad as np
from agent.workstreams import Scope
np.NOTEPAD_FILE = Path(sys.argv[2])
sc = Scope("/home/p1", "proj-a", "private", "root-1")
wid, mode, who = sys.argv[3], sys.argv[4], sys.argv[5]
import time
while time.time() < float(sys.argv[6]):
    time.sleep(0.001)
if mode == "claim":
    got = np.claim_occurrence(sc, wid, occurrence_key="h", claimer=who)
    print("WIN" if got else "LOSE")
elif mode == "cas":
    try:
        np.update_workstream(sc, wid, expected_revision=1, summary="by-" + who)
        print("WIN")
    except np.StaleRevision:
        print("LOSE")
elif mode == "budget":
    try:
        np.spend_budget(sc, wid, turns=1)
        print("WIN")
    except np.BudgetExhausted:
        print("LOSE")
"""


def _race(db, mode, n=6, setup=None):
    import subprocess
    import sys
    import time
    from pathlib import Path

    sc = scope()
    wid = make(sc)["workstream_id"]
    if setup:
        setup(sc, wid)
    repo = str(Path(__file__).resolve().parents[2])
    go = time.time() + 1.5
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, repo, str(db), wid, mode, f"c{i}", str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for i in range(n)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        outs.append(out.strip())
    return sc, wid, outs


class TestCrossProcess:
    def test_claim_exactly_one_process_wins(self, db):
        def setup(sc, wid):
            np.ensure_occurrence(sc, wid, occurrence_key="h", now=NOW)

        sc, wid, outs = _race(db, "claim", setup=setup)
        assert outs.count("WIN") == 1
        assert outs.count("LOSE") == 5

    def test_cas_exactly_one_process_wins(self, db):
        sc, wid, outs = _race(db, "cas")
        assert outs.count("WIN") == 1
        assert np.get_workstream(sc, wid)["revision"] == 2

    def test_budget_never_overspent_across_processes(self, db):
        def setup(sc, wid):
            np.link_authority(sc, wid, plan_ref={"d": "1"}, budget={"turns": 2}, now=NOW)

        sc, wid, outs = _race(db, "budget", setup=setup)
        assert outs.count("WIN") == 2
        assert np.get_authority(sc, wid)["budget"]["turns"] == 0

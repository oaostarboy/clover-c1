"""SessionDB human-origin receipt storage: exact-row binding and fail-closed reads.

Real SessionDB and the real mint/redeem producer; no fakes.  Provenance only:
a receipt never authorizes anything and a role/content/id is never evidence.
"""

import dataclasses
import sqlite3
import time

import pytest

from agent import workstream_origin as origin
from clover_state import SessionDB

TABLE = "human_origin_receipts"


class _Event:
    """Stand-in for an adapter-built event object (weak-referenceable)."""

    internal = False


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(db_path=tmp_path / "state.db")
    handle.create_session("root-sess", source="telegram")
    yield handle
    handle.close()


def _profile():
    from agent.workstreams import current_profile

    return current_profile()


def _capability(session_id="root-sess", *, current=lambda: True, profile=None):
    event = _Event()
    assert origin.mint_human_ingress(event, platform="telegram")
    ingress = origin.redeem_human_ingress(
        event, platform="telegram", profile=profile or _profile(),
        session_id=session_id, session_key="key", run_generation=1, is_current=current,
    )
    assert ingress is not None
    return ingress, event


def _write(db, session_id, ingress, *, role="user", content="remember this"):
    """Persist one row through the real batch writer; return its row id."""
    row = {"role": role, "content": content, "human_ingress": ingress}
    db.append_messages_batch(session_id, [row])
    return row["_row_id"]


def _tables(db):
    return {r[0] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_receipt_binds_exact_user_row_and_reads_from_fresh_handle(db, tmp_path):
    ingress, _event = _capability()
    row_id = _write(db, "root-sess", ingress)

    receipt = db.get_human_origin("root-sess", row_id)
    assert (receipt.profile, receipt.session_id, receipt.message_row_id) == (
        _profile(), "root-sess", row_id)
    assert receipt.ingress_event_id == ingress.event_id
    assert (receipt.origin, receipt.consumed) == ("human", False)

    fresh = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert fresh.get_human_origin("root-sess", row_id) == receipt
    finally:
        fresh.close()


def test_no_table_or_write_without_a_capability(db):
    db.append_messages_batch("root-sess", [{"role": "user", "content": "plain"}])
    assert db.get_human_origin("root-sess", 1) is None
    assert TABLE not in _tables(db)


def test_forged_or_altered_capability_writes_nothing(db):
    ingress, _event = _capability()
    forged = dataclasses.replace(ingress, event_id="attacker-chosen")  # seal now wrong
    unsealed = dataclasses.replace(ingress, seal="")
    handbuilt = origin.HumanIngress(
        event_id="e", platform="telegram", profile=_profile(), session_id="root-sess",
        session_key="k", run_generation=1, seal="0" * 64,
    )
    for bad in (forged, unsealed, handbuilt, "human", {"event_id": "x"}, True):
        row_id = _write(db, "root-sess", bad)
        assert db.get_human_origin("root-sess", row_id) is None
    assert TABLE not in _tables(db)


def test_only_user_rows_without_tool_call_id_get_a_receipt(db):
    ingress, _event = _capability()
    assistant_id = _write(db, "root-sess", ingress, role="assistant")
    tool_row = {"role": "user", "content": "x", "tool_call_id": "call-1", "human_ingress": ingress}
    db.append_messages_batch("root-sess", [tool_row])
    assert db.get_human_origin("root-sess", assistant_id) is None
    assert db.get_human_origin("root-sess", tool_row["_row_id"]) is None
    assert TABLE not in _tables(db)


def test_session_and_row_mismatch_return_none(db):
    ingress, _event = _capability()
    row_id = _write(db, "root-sess", ingress)
    db.create_session("other-sess", source="telegram")
    assert db.get_human_origin("other-sess", row_id) is None
    assert db.get_human_origin("root-sess", row_id + 1) is None
    assert db.get_human_origin("root-sess", "1") is None
    assert db.get_human_origin("root-sess", True) is None
    assert db.get_human_origin("", row_id) is None


def test_capability_from_a_foreign_conversation_root_is_rejected(db):
    db.create_session("foreign-root", source="telegram")
    ingress, _event = _capability(session_id="foreign-root")
    row_id = _write(db, "root-sess", ingress)
    assert db.get_human_origin("root-sess", row_id) is None


def test_compression_child_of_the_same_root_is_accepted(db):
    db.create_session("child-sess", source="telegram", parent_session_id="root-sess")
    ingress, _event = _capability(session_id="root-sess")
    row_id = _write(db, "child-sess", ingress)
    receipt = db.get_human_origin("child-sess", row_id)
    assert receipt is not None and receipt.session_id == "child-sess"


def test_foreign_profile_capability_and_reader_are_rejected(db, monkeypatch, tmp_path):
    foreign, _event = _capability(profile=str(tmp_path / "another-profile"))
    row_id = _write(db, "root-sess", foreign)
    assert db.get_human_origin("root-sess", row_id) is None

    ingress, _event = _capability()
    row_id = _write(db, "root-sess", ingress)
    assert db.get_human_origin("root-sess", row_id) is not None
    other = tmp_path / "second-home"
    other.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(other))
    assert db.get_human_origin("root-sess", row_id) is None


def test_stopped_turn_is_rejected_and_replay_is_single_use(db):
    live = {"ok": True}
    ingress, _event = _capability(current=lambda: live["ok"])
    live["ok"] = False  # Stop / new run generation
    stopped_id = _write(db, "root-sess", ingress)
    assert db.get_human_origin("root-sess", stopped_id) is None

    live["ok"] = True
    first = _write(db, "root-sess", ingress)
    assert db.get_human_origin("root-sess", first) is not None
    replay = _write(db, "root-sess", ingress)  # same capability, second row
    assert db.get_human_origin("root-sess", replay) is None
    count = db._conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    assert count == 1


def test_retired_capability_is_dead(db):
    ingress, _event = _capability()
    origin.retire_human_ingress(ingress)
    row_id = _write(db, "root-sess", ingress)
    assert db.get_human_origin("root-sess", row_id) is None


def test_consume_is_atomic_single_use_and_hides_the_receipt(db):
    ingress, _event = _capability()
    row_id = _write(db, "root-sess", ingress)
    assert db.consume_human_origin("root-sess", row_id, consumer="  ") is False
    assert db.consume_human_origin("root-sess", row_id, consumer="feedback") is True
    assert db.get_human_origin("root-sess", row_id) is None
    assert db.consume_human_origin("root-sess", row_id, consumer="feedback") is False


def test_expired_receipt_is_unknown(db):
    ingress, _event = _capability()
    row_id = _write(db, "root-sess", ingress)
    db._conn.execute(f"UPDATE {TABLE} SET expires_at = ?", (time.time() - 1,))
    db._conn.commit()
    assert db.get_human_origin("root-sess", row_id) is None
    assert db.consume_human_origin("root-sess", row_id, consumer="feedback") is False


def test_inactive_or_removed_row_loses_its_receipt(db):
    ingress, _event = _capability()
    row_id = _write(db, "root-sess", ingress)
    db._conn.execute("UPDATE messages SET active = 0 WHERE id = ?", (row_id,))
    db._conn.commit()
    assert db.get_human_origin("root-sess", row_id) is None
    db._conn.execute("UPDATE messages SET active = 1 WHERE id = ?", (row_id,))
    db._conn.execute("UPDATE messages SET role = 'assistant' WHERE id = ?", (row_id,))
    db._conn.commit()
    assert db.get_human_origin("root-sess", row_id) is None


def test_receipt_row_cannot_be_planted_for_a_role_user_row(db):
    """Writing a matching receipt row by hand still cannot make a *different*
    persisted row human: the receipt is keyed to (session, row id)."""
    ingress, _event = _capability()
    real = _write(db, "root-sess", ingress)
    decoy = db.append_message("root-sess", "user", "remember this")  # same text
    assert db.get_human_origin("root-sess", real) is not None
    assert db.get_human_origin("root-sess", decoy) is None
    with sqlite3.connect(db.db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1


def test_existing_workstream_reader_contract_accepts_only_the_real_receipt(db):
    """Sonnet's HumanOriginReader / verified_human_origin consume the receipt."""
    from agent import workstreams

    ingress, _event = _capability()
    row_id = _write(db, "root-sess", ingress)
    receipt = db.get_human_origin("root-sess", row_id)

    verified = workstreams.verified_human_origin(
        receipt, db=db, human_reader=origin.read_human_origin)
    assert verified == receipt

    forged = dataclasses.replace(receipt, ingress_event_id="guessed-id")
    assert workstreams.verified_human_origin(
        forged, db=db, human_reader=origin.read_human_origin) is None
    decoy = dataclasses.replace(receipt, message_row_id=row_id + 1)
    assert workstreams.verified_human_origin(
        decoy, db=db, human_reader=origin.read_human_origin) is None

    # Once consumed the receipt no longer verifies, and the default (unwired)
    # reader still fails closed.
    assert db.consume_human_origin("root-sess", row_id, consumer="test") is True
    assert workstreams.verified_human_origin(
        receipt, db=db, human_reader=origin.read_human_origin) is None
    assert workstreams._HUMAN_ORIGIN_READER is None

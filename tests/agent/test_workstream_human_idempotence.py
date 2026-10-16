"""One human ingress event produces at most one durable effect per operation,
even across a crash between the write and the receipt consumption."""

import pytest

from agent import workstream_origin as origin
from agent import workstreams as ws
from clover_state import SessionDB
from cron import notepad as np


class _Event:
    internal = False


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    db.create_session("s1", source="telegram")
    scope = ws.make_scope(str(tmp_path), audience="private:1", root="1")
    settings = ws.ProactiveSettings(enabled=True, mode="active", suggestions_enabled=True,
                                    recall_enabled=True)
    yield db, scope, settings
    db.close()


def _human(db):
    """A real receipt for a real persisted user row (real mint/redeem/write)."""
    event = _Event()
    assert origin.mint_human_ingress(event, platform="telegram")
    ingress = origin.redeem_human_ingress(
        event, platform="telegram", profile=ws.current_profile(), session_id="s1",
        session_key="k", run_generation=1, is_current=lambda: True)
    row = {"role": "user", "content": "x", "human_ingress": ingress}
    db.append_messages_batch("s1", [row])
    receipt = db.get_human_origin("s1", row["_row_id"])
    assert receipt is not None
    return receipt


def _record(scope, settings):
    return np.create_workstream(scope, kind="obligation", title="t", summary="s",
                                fingerprint="fp-1", approved=True)


@pytest.mark.parametrize("kind", ["accept", "reject", "defer", "done", "stop"])
def test_replayed_event_yields_one_feedback_row_for_every_kind(host, kind):
    db, scope, settings = host
    wid = _record(scope, settings)["workstream_id"]
    receipt = _human(db)
    first = ws.record_feedback(scope, wid, kind=kind, settings=settings, origin=receipt,
                               human_reader=origin.read_human_origin, db=db)
    again = ws.record_feedback(scope, wid, kind=kind, settings=settings, origin=receipt,
                               human_reader=origin.read_human_origin, db=db)
    assert first is not None and again is not None
    assert again["feedback_id"] == first["feedback_id"]
    assert len(np.list_feedback(scope, wid)) == 1


def test_failure_inside_the_feedback_transaction_leaves_nothing_then_replays_once(
    host, monkeypatch,
):
    db, scope, settings = host
    wid = _record(scope, settings)["workstream_id"]
    receipt = _human(db)
    real = np._new_id

    def _fail_on_receipt_id(prefix):
        if prefix == "rc":
            raise RuntimeError("simulated crash while writing the receipt")
        return real(prefix)

    monkeypatch.setattr(np, "_new_id", _fail_on_receipt_id)
    with pytest.raises(RuntimeError):
        ws.record_feedback(scope, wid, kind="done", settings=settings, origin=receipt,
                           human_reader=origin.read_human_origin, db=db)
    assert np.list_feedback(scope, wid) == []  # rolled back, nothing half-written
    monkeypatch.setattr(np, "_new_id", real)
    first = ws.record_feedback(scope, wid, kind="done", settings=settings, origin=receipt,
                               human_reader=origin.read_human_origin, db=db)
    again = ws.record_feedback(scope, wid, kind="done", settings=settings, origin=receipt,
                               human_reader=origin.read_human_origin, db=db)
    assert again["feedback_id"] == first["feedback_id"]
    assert len(np.list_feedback(scope, wid)) == 1


def test_replayed_targeted_correction_returns_the_prior_replacement(host):
    db, scope, settings = host
    target = _record(scope, settings)["workstream_id"]
    receipt = _human(db)
    kw = dict(text="use the v2 endpoint", settings=settings, origin=receipt,
              human_reader=origin.read_human_origin, db=db, supersedes_id=target,
              session_id="s1", approved=True)
    first = ws.record_correction(scope, **kw)
    again = ws.record_correction(scope, **kw)
    assert again is not None and again["workstream_id"] == first["workstream_id"]
    open_corrections = np.list_workstreams(scope, statuses=["open"], kinds=["correction"])
    assert len(open_corrections) == 1


def test_replayed_untargeted_correction_is_one_record(host):
    db, scope, settings = host
    receipt = _human(db)
    kw = dict(text="the target is staging-2", settings=settings, origin=receipt,
              human_reader=origin.read_human_origin, db=db, session_id="s1", approved=True)
    first = ws.record_correction(scope, **kw)
    again = ws.record_correction(scope, **kw)
    assert again["workstream_id"] == first["workstream_id"]
    assert len(np.list_workstreams(scope, kinds=["correction"])) == 1

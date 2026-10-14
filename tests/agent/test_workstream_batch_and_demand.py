"""Notepad write batches and the deterministic evidence-demand trigger."""

import pytest

from agent import workstreams as ws
from agent.workstream_evidence import evidence_context, is_evidence_request
from cron import notepad as np


@pytest.fixture
def scope(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    return ws.make_scope(str(tmp_path), audience="private:1", root="1")


def _create(scope, fp):
    return np.create_workstream(scope, kind="obligation", title=fp, summary=fp,
                                fingerprint=fp, approved=True)


def test_batch_commits_everything_and_reads_its_own_writes(scope):
    with np.write_batch():
        first = _create(scope, "a")
        assert np.get_workstream(scope, first["workstream_id"]) is not None
        _create(scope, "b")
    assert {r["fingerprint"] for r in np.list_workstreams(scope)} == {"a", "b"}


def test_batch_rolls_back_every_write_on_error(scope):
    keep = _create(scope, "keep")
    with pytest.raises(RuntimeError):
        with np.write_batch():
            _create(scope, "a")
            np.add_feedback(scope, keep["workstream_id"], kind="accept", actor="user")
            raise RuntimeError("boom")
    assert [r["fingerprint"] for r in np.list_workstreams(scope)] == ["keep"]
    assert np.list_feedback(scope, keep["workstream_id"]) == []


def test_writes_outside_a_batch_still_commit_individually(scope):
    _create(scope, "solo")
    assert len(np.list_workstreams(scope)) == 1


@pytest.mark.parametrize("text, expected", [
    ("Did the nightly backup run?", True),
    ("Has the deploy finished yet?", True),
    ("Is the migration still running?", True),
    ("What is the status of the release?", True),
    ("Which deployment target is configured?", False),
    ("The backup ran fine.", False),
    ("Thanks, that looks good", False),
    ("", False),
    (None, False),
    ("Did it run? " + "x" * 600, False),
])
def test_evidence_request_trigger_is_deterministic_and_narrow(text, expected):
    assert is_evidence_request(text) is expected


def test_non_request_returns_no_evidence_without_touching_owners(scope):
    assert evidence_context(object(), scope, "Which target is configured?") == ""

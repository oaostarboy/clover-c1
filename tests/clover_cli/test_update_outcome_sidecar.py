"""The update outcome sidecar must actually exist and be callable.

Reported from a Windows install on 2026-08-31. ``clover update`` stopped the
gateway four times in under 24 hours and left no completion record at all —
no success, no failure, nothing. The operator could not tell whether the
update had finished, crashed, or been killed.

Root cause: ``update_receipt.finalize_update_receipt`` imports four names
from ``clover_cli.update_lock`` that do not exist there::

    from clover_cli.update_lock import (
        OUTCOME_FAILED, OUTCOME_ROLLED_BACK, OUTCOME_SUCCESS,
        record_update_outcome,
    )

The import sits inside ``try: ... except Exception: logger.debug(...)``, so
every call raised ImportError, was swallowed at debug level, and the sidecar
was never written. The code looked present and did nothing.

These tests bind the contract in both directions: the names must exist, and
the sidecar must land on disk after a real finalize call.
"""

from __future__ import annotations

import json

import pytest


class TestOutcomeSidecarNamesExist:
    """The names update_receipt imports must be importable."""

    def test_outcome_constants_are_importable(self):
        from clover_cli.update_lock import (  # noqa: F401
            OUTCOME_FAILED,
            OUTCOME_ROLLED_BACK,
            OUTCOME_SUCCESS,
        )

    def test_record_update_outcome_is_importable(self):
        from clover_cli.update_lock import record_update_outcome  # noqa: F401

        assert callable(record_update_outcome)

    def test_outcome_constants_are_distinct(self):
        from clover_cli.update_lock import (
            OUTCOME_FAILED,
            OUTCOME_ROLLED_BACK,
            OUTCOME_SUCCESS,
        )

        assert len({OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_ROLLED_BACK}) == 3


class TestOutcomeSidecarIsWritten:
    """A finalized update must leave a readable record of how it ended."""

    @pytest.fixture()
    def clover_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "clover_constants.get_process_clover_home", lambda: tmp_path
        )
        return tmp_path

    def test_record_update_outcome_writes_a_readable_file(self, clover_home):
        from clover_cli.update_lock import (
            OUTCOME_SUCCESS,
            outcome_path,
            read_last_outcome,
            record_update_outcome,
        )

        record_update_outcome(OUTCOME_SUCCESS, detail="all good", started_at=1000)

        assert outcome_path().exists()
        data = json.loads(outcome_path().read_text(encoding="utf-8"))
        assert data["outcome"] == OUTCOME_SUCCESS
        assert data["detail"] == "all good"

        parsed = read_last_outcome()
        assert parsed is not None
        assert parsed["outcome"] == OUTCOME_SUCCESS

    def test_killed_update_is_distinguishable_from_a_clean_one(self, clover_home):
        """The whole point: 'died mid-update' must not read as 'never ran'."""
        from clover_cli.update_lock import read_last_outcome

        assert read_last_outcome() is None  # nothing has run yet

        from clover_cli.update_lock import OUTCOME_FAILED, record_update_outcome

        record_update_outcome(OUTCOME_FAILED, detail="killed during venv sync")

        parsed = read_last_outcome()
        assert parsed is not None
        assert parsed["outcome"] == OUTCOME_FAILED
        assert "killed" in parsed["detail"]

    def test_finalize_update_receipt_writes_the_sidecar(self, clover_home, tmp_path, monkeypatch):
        """End-to-end: the real caller must produce the record.

        This is the test that would have caught the bug. The unit above
        proves the names exist; this proves the swallowed-ImportError path
        is genuinely gone from the caller.
        """
        from clover_cli import update_receipt
        from clover_cli.update_lock import OUTCOME_SUCCESS, read_last_outcome

        monkeypatch.setattr(
            update_receipt, "_receipt_dir", lambda: tmp_path / "receipts"
        )
        update_receipt.begin_update_receipt()
        update_receipt.record_step("sync", True, "ok")
        update_receipt.finalize_update_receipt("success")

        parsed = read_last_outcome()
        assert parsed is not None, (
            "finalize_update_receipt must write the outcome sidecar — "
            "a swallowed ImportError here is the reported bug"
        )
        assert parsed["outcome"] == OUTCOME_SUCCESS

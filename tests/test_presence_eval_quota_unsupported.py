"""Unsupported quota platforms hold before filesystem access, never emulate POSIX."""
import os

import pytest


def test_isolated_quota_unsupported_platform_holds_before_open(monkeypatch, tmp_path):
    from scripts.eval_presence_current_turn import _reserve_isolated_call
    from clover_cli.readonly_files import ReadOnlyStateError
    # On Windows this is the native OS branch; on POSIX explicitly exercise the
    # same unsupported branch. Never alter geteuid or Windows reader behavior.
    before = sorted(p.name for p in tmp_path.iterdir())
    monkeypatch.setattr(os, 'name', 'nt')
    with pytest.raises(ReadOnlyStateError, match='secure_evaluation_quota_unsupported'):
        _reserve_isolated_call(str(tmp_path/'missing'), 'SCRIPTED', 3)
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == before

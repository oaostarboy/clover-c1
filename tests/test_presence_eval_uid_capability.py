"""Absent POSIX UID capability never grants a quota object or writer."""
import json
import os
from pathlib import Path

import pytest

from clover_cli.readonly_files import ReadOnlyStateError
from scripts.eval_presence_current_turn import _reserve_isolated_call
from scripts.presence_eval_guard import ReadOnlyEvaluationGuard


def test_missing_uid_refuses_write_root_before_inventory(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    quota = tmp_path / 'quota'
    quota.mkdir(mode=0o700)
    monkeypatch.delattr(os, 'geteuid', raising=False)
    with pytest.raises(PermissionError, match='insecure_evaluation_directory'):
        ReadOnlyEvaluationGuard(home=home, source=Path(__file__).resolve().parents[1], write_dirs=[quota])
    assert list(quota.iterdir()) == []


def test_missing_uid_refuses_reservation_without_mutation(monkeypatch, tmp_path):
    quota = tmp_path / 'quota'
    quota.mkdir(mode=0o700)
    leaf = quota / 'presence-eval-quota.json'
    before = json.dumps({'version': 1, 'binding': 'synthetic-owner', 'used': 0}).encode()
    leaf.write_bytes(before)
    leaf.chmod(0o600)
    monkeypatch.delattr(os, 'geteuid', raising=False)
    with pytest.raises(ReadOnlyStateError, match='secure_evaluation_quota_unsupported'):
        _reserve_isolated_call(quota, 'synthetic-owner', 3)
    assert leaf.read_bytes() == before
    assert sorted(p.name for p in quota.iterdir()) == ['presence-eval-quota.json']


def test_missing_uid_denies_anchored_write_even_with_cached_root(monkeypatch, tmp_path):
    quota = tmp_path / 'quota'
    quota.mkdir(mode=0o700)
    leaf = quota / 'presence-eval-quota.json'
    leaf.write_bytes(b'unchanged')
    leaf.chmod(0o600)
    info = quota.stat()
    guard = object.__new__(ReadOnlyEvaluationGuard)
    guard._write_roots = {quota: (info.st_dev, info.st_ino)}
    monkeypatch.delattr(os, 'geteuid', raising=False)
    assert guard._write_allowed(str(leaf), leaf) is False
    assert leaf.read_bytes() == b'unchanged'


def test_missing_uid_preserves_readonly_guard_with_no_write_roots(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    monkeypatch.delattr(os, 'geteuid', raising=False)
    guard = ReadOnlyEvaluationGuard(home=home, source=Path(__file__).resolve().parents[1], write_dirs=[])
    assert guard._write_roots == {}
    assert guard.receipt()['baseline_unchanged'] is True

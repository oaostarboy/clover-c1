"""Failure-atomic Linux catch-up receipt handoff, with only tmp_path I/O."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clover_cli import linux_catchup_handoff as handoff


_SHA = "f" * 40
_FLEET = [{"profile": "default", "pid": 301, "state": "current", "code_sha": _SHA}]


def _pending(tmp_path):
    receipt = tmp_path / "latest.json"
    receipt.write_text(json.dumps({
        "started_at": "2026-10-02T12:00:00+00:00", "pid": 10,
        "outcome": "running", "post_update": {},
        "linux_systemd_catchup": {
            "version": 1, "clover_home": str(tmp_path.resolve()),
            "expected_sha": _SHA, "targets": [{"profile": "default", "pid": 101}],
        },
    }), encoding="utf-8")
    marker = tmp_path / "fleet_restart_pending"
    marker.write_bytes(b"expected_sha=" + _SHA.encode())
    return receipt, marker


def test_marker_clear_failure_keeps_receipt_provisional(monkeypatch, tmp_path):
    receipt, marker = _pending(tmp_path)
    before = receipt.read_bytes()
    marker_bytes = marker.read_bytes()
    real_unlink = Path.unlink

    def fail_marker_unlink(self, *args, **kwargs):
        if self == marker:
            raise OSError("injected marker unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker_unlink)
    assert handoff.verify_and_finalize_receipt(receipt, clover_home=tmp_path, fleet=_FLEET) is False
    assert receipt.read_bytes() == before
    assert marker.read_bytes() == marker_bytes
    assert not list(tmp_path.glob("latest.json.*.tmp"))


def test_receipt_commit_failure_restores_restart_marker(monkeypatch, tmp_path):
    receipt, marker = _pending(tmp_path)
    before = receipt.read_bytes()
    marker_bytes = marker.read_bytes()
    real_replace = Path.replace

    def fail_receipt_commit(self, target):
        if target == receipt:
            assert not marker.exists()  # failure is injected after clear
            raise OSError("injected receipt replace failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_receipt_commit)
    assert handoff.verify_and_finalize_receipt(receipt, clover_home=tmp_path, fleet=_FLEET) is False
    assert receipt.read_bytes() == before
    assert marker.read_bytes() == marker_bytes
    assert not list(tmp_path.glob("latest.json.*.tmp"))


def test_receipt_commit_failure_restores_missing_marker_breadcrumb(monkeypatch, tmp_path):
    receipt, marker = _pending(tmp_path)
    marker.unlink()
    before = receipt.read_bytes()

    def fail_receipt_commit(self, target):
        if target == receipt:
            raise OSError("injected receipt replace failure")
        pytest.fail("unexpected replace target")

    monkeypatch.setattr(Path, "replace", fail_receipt_commit)
    assert handoff.verify_and_finalize_receipt(receipt, clover_home=tmp_path, fleet=_FLEET) is False
    assert receipt.read_bytes() == before
    assert marker.read_bytes() == b"expected_sha=" + _SHA.encode()
    assert not list(tmp_path.glob("latest.json.*.tmp"))


def test_preparation_write_failure_keeps_restart_obligation(monkeypatch, tmp_path):
    receipt, marker = _pending(tmp_path)
    before = receipt.read_bytes()
    marker_bytes = marker.read_bytes()
    real_write_text = Path.write_text

    def fail_temp_write(self, text, *args, **kwargs):
        if self.name.startswith("latest.json.") and self.name.endswith(".tmp"):
            raise OSError("injected temp write failure")
        return real_write_text(self, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_temp_write)
    assert handoff.verify_and_finalize_receipt(receipt, clover_home=tmp_path, fleet=_FLEET) is False
    assert receipt.read_bytes() == before
    assert marker.read_bytes() == marker_bytes
    assert not list(tmp_path.glob("latest.json.*.tmp"))


def test_marker_read_failure_keeps_restart_obligation(monkeypatch, tmp_path):
    receipt, marker = _pending(tmp_path)
    before = receipt.read_bytes()
    marker_bytes = marker.read_bytes()
    real_read_bytes = Path.read_bytes

    def fail_marker_read(self):
        if self == marker:
            raise OSError("injected marker read failure")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fail_marker_read)
    assert handoff.verify_and_finalize_receipt(receipt, clover_home=tmp_path, fleet=_FLEET) is False
    assert receipt.read_bytes() == before
    assert marker.exists()
    assert marker.open("rb").read() == marker_bytes
    assert not list(tmp_path.glob("latest.json.*.tmp"))

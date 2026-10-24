"""Offline model of SUNI's post-pull missing-owner refusal; no Mac access."""
import json

import pytest


def test_macos_156_postpull_missing_owner_defers_before_dependency_mutation(monkeypatch, tmp_path, capsys):
    from clover_cli import gateway, managed_uv, update_cmd, update_delivery_guard as guard, update_receipt

    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("sys.argv", ["clover", "update", "--yes"])
    monkeypatch.setattr(update_receipt, "_current", None)
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kw: [])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [40563])
    monkeypatch.setattr(update_cmd, "_gateway_pid_install_verdict", lambda pid: "own")
    monkeypatch.setattr(guard, "_proved_stderr_wrapper", lambda pid, admitted: False)
    pending = tmp_path / "pending-restart"
    snapshot = tmp_path / "snapshot"
    source = tmp_path / "already-pulled-source"
    pending.write_bytes(b"preserved restart intent")
    snapshot.write_bytes(b"preserved rollback evidence")
    source.write_bytes(b"new source already installed before this seam")

    def mutation(*a, **kw):
        pytest.fail("dependency mutation must not run without owner-bound status")

    monkeypatch.setattr(managed_uv, "resolve_uv", mutation)
    monkeypatch.setattr(managed_uv, "ensure_uv", mutation)
    # The released 1.5.6 caller imports and invokes this API after its pull.
    # This is a seam test, not a full Mac updater or native process-discovery test.
    with pytest.raises(SystemExit) as exc:
        managed_uv.update_managed_uv()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "PID 40563: no owner-bound gateway status" in out
    assert "Source or dependencies may already have changed" in out
    assert pending.read_bytes() == b"preserved restart intent"
    assert snapshot.read_bytes() == b"preserved rollback evidence"
    assert source.read_bytes() == b"new source already installed before this seam"
    receipt = json.loads((tmp_path / "logs/update_receipts/latest.json").read_text())
    assert receipt["outcome"] == "failed"
    assert receipt["delivery_restart_guard"]["status"] == "deferred"

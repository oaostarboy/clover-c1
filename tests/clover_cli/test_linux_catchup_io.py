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


def _durable_pending(tmp_path):
    receipt, marker = _pending(tmp_path)
    latest = tmp_path / "logs" / "update_receipts" / "latest.json"
    latest.parent.mkdir(parents=True)
    receipt.replace(latest)
    return latest, marker


def _assert_durable_obligation(monkeypatch, tmp_path):
    from clover_cli import update_cmd
    monkeypatch.setattr(update_cmd.sys, "platform", "linux")
    monkeypatch.setattr(handoff, "_supported", lambda: True)
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_receipt_reports_stale_runtime", lambda: False)
    assert update_cmd._pending_fleet_restart_needed()


def test_stopped_systemd_target_keeps_tagged_restart_obligation(monkeypatch, tmp_path):
    from clover_cli import update_cmd

    receipt, marker = _durable_pending(tmp_path)
    monkeypatch.setattr(update_cmd.sys, "platform", "linux")
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_receipt_reports_stale_runtime", lambda: False)
    monkeypatch.setattr(update_cmd, "_pending_fleet_restart_needed", lambda: True)
    monkeypatch.setattr(update_cmd, "_owned_systemd_service_pids", lambda: set())
    monkeypatch.setattr("clover_cli.gateway.supports_systemd_services", lambda: True)
    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", lambda: True)

    with pytest.raises(SystemExit) as result:
        update_cmd._apply_pending_fleet_restart_catchup()

    assert result.value.code != 0
    assert marker.exists()
    assert handoff.pending_receipt_requires_catchup(receipt, clover_home=tmp_path)
    assert json.loads(receipt.read_text())["linux_systemd_catchup"]["targets"] == [
        {"profile": "default", "pid": 101}
    ]


def test_interruption_after_marker_clear_keeps_durable_obligation(monkeypatch, tmp_path):
    receipt, marker = _durable_pending(tmp_path)

    def interrupt_commit(self, target):
        assert target == receipt
        assert not marker.exists()
        raise SystemExit("injected process interruption before commit")

    monkeypatch.setattr(Path, "replace", interrupt_commit)
    with pytest.raises(SystemExit):
        handoff.verify_and_finalize_receipt(receipt, clover_home=tmp_path, fleet=_FLEET)
    assert json.loads(receipt.read_text())["outcome"] == "running"
    assert not marker.exists()
    _assert_durable_obligation(monkeypatch, tmp_path)


def test_commit_and_restore_failure_keep_durable_obligation(monkeypatch, tmp_path):
    receipt, marker = _durable_pending(tmp_path)
    real_write_bytes = Path.write_bytes

    def fail_commit(self, target):
        assert target == receipt
        raise OSError("injected commit failure")

    def fail_restore(self, data):
        if self == marker:
            raise OSError("injected marker restoration failure")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "replace", fail_commit)
    monkeypatch.setattr(Path, "write_bytes", fail_restore)
    assert not handoff.verify_and_finalize_receipt(receipt, clover_home=tmp_path, fleet=_FLEET)
    assert json.loads(receipt.read_text())["outcome"] == "running"
    assert not marker.exists()
    _assert_durable_obligation(monkeypatch, tmp_path)


@pytest.mark.parametrize("case", ["success", "rolled-back", "foreign-home", "invalid-sha", "empty-targets"])
def test_durable_lookup_rejects_finished_or_invalid_intent(monkeypatch, tmp_path, case):
    receipt, marker = _durable_pending(tmp_path)
    marker.unlink()
    record = json.loads(receipt.read_text())
    tag = record["linux_systemd_catchup"]
    if case in {"success", "rolled-back"}:
        record["outcome"] = case
    elif case == "foreign-home":
        tag["clover_home"] = str(tmp_path / "foreign")
    elif case == "invalid-sha":
        tag["expected_sha"] = "not-a-sha"
    else:
        tag["targets"] = []
    receipt.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(handoff, "_supported", lambda: True)
    assert not handoff.pending_receipt_requires_catchup(receipt, clover_home=tmp_path)


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_non_linux_pending_lookup_skips_new_intent_reader(monkeypatch, tmp_path, platform):
    from clover_cli import update_cmd
    receipt, marker = _durable_pending(tmp_path)
    marker.unlink()
    monkeypatch.setattr(update_cmd.sys, "platform", platform)
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_receipt_reports_stale_runtime", lambda: False)
    monkeypatch.setattr(handoff, "pending_receipt_requires_catchup",
                        lambda *args, **kwargs: pytest.fail("non-Linux intent reader"))
    assert not update_cmd._pending_fleet_restart_needed()


@pytest.mark.parametrize("marker_present", [True, False])
def test_command_receipt_boundary_preserves_stopped_target(monkeypatch, tmp_path, marker_present):
    from clover_cli import config, update_cmd, update_receipt

    monkeypatch.setattr(update_receipt.sys, "platform", "linux")

    latest, marker = _durable_pending(tmp_path)
    if not marker_present:
        marker.unlink()
    original_tag = json.loads(latest.read_text())["linux_systemd_catchup"]
    monkeypatch.setattr(config, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_receipt_reports_stale_runtime", lambda: False)
    monkeypatch.setattr(update_cmd, "_owned_systemd_service_pids", lambda: set())
    monkeypatch.setattr("clover_cli.gateway.supports_systemd_services", lambda: True)
    monkeypatch.setattr(handoff, "_supported", lambda: True)
    monkeypatch.setattr(update_receipt, "_current", None)
    update_receipt.begin_update_receipt()
    with pytest.raises(SystemExit) as failure:
        update_cmd._apply_pending_fleet_restart_catchup()
    assert failure.value.code == 1
    update_receipt.finalize_pending_update_receipt(failure.value.code)
    saved = json.loads(latest.read_text())
    assert saved["outcome"] != "success"
    assert saved.get("linux_systemd_catchup") == original_tag
    assert handoff.pending_receipt_requires_catchup(latest, clover_home=tmp_path)


def test_pending_receipt_cannot_forget_down_profile_during_prepare(monkeypatch, tmp_path):
    from clover_cli import build_info, config, update_receipt

    monkeypatch.setattr(update_receipt.sys, "platform", "linux")

    latest, _ = _durable_pending(tmp_path)
    record = json.loads(latest.read_text())
    record["linux_systemd_catchup"]["targets"].append({"profile": "other", "pid": 102})
    latest.write_text(json.dumps(record))
    monkeypatch.setattr(config, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(handoff, "_supported", lambda: True)
    monkeypatch.setattr(build_info, "get_code_identity", lambda **kwargs: {"sha": _SHA})
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", lambda **kwargs: [
        {"profile": "default", "pid": 101, "state": "current", "code_sha": _SHA}
    ])
    monkeypatch.setattr(update_receipt, "_current", None)
    update_receipt.begin_update_receipt()
    assert not handoff.prepare_pending_receipt(update_receipt, clover_home=tmp_path, pre_restart_pids={101})
    update_receipt.finalize_pending_update_receipt(1)
    assert json.loads(latest.read_text())["linux_systemd_catchup"]["targets"] == record["linux_systemd_catchup"]["targets"]


def test_generic_success_cannot_retire_inherited_unverified_intent(monkeypatch, tmp_path):
    from clover_cli import config, update_receipt

    monkeypatch.setattr(update_receipt.sys, "platform", "linux")

    latest, marker = _durable_pending(tmp_path)
    marker.unlink()
    monkeypatch.setattr(config, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(handoff, "_supported", lambda: True)
    monkeypatch.setattr(update_receipt, "_current", None)
    update_receipt.begin_update_receipt()
    update_receipt.finalize_pending_update_receipt(0)
    assert json.loads(latest.read_text())["outcome"] != "success"
    assert handoff.pending_receipt_requires_catchup(latest, clover_home=tmp_path)


def test_verified_catchup_stays_success_at_command_boundary(monkeypatch, tmp_path):
    from clover_cli import build_info, config, update_cmd, update_receipt

    monkeypatch.setattr(update_receipt.sys, "platform", "linux")

    latest, marker = _durable_pending(tmp_path)
    monkeypatch.setattr(config, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(handoff, "_supported", lambda: True)
    monkeypatch.setattr(build_info, "get_code_identity", lambda **kwargs: {"sha": _SHA})
    monkeypatch.setattr(update_receipt, "_current", None)
    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", lambda: True)
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", lambda **kwargs: (
        [{"profile": "default", "pid": 101, "state": "current", "code_sha": _SHA}]
        if kwargs.get("pre_restart_pids") else _FLEET
    ))
    update_receipt.begin_update_receipt()
    update_cmd._apply_linux_systemd_catchup({101})
    update_receipt.finalize_pending_update_receipt(0)
    saved = json.loads(latest.read_text())
    assert saved["outcome"] == "success"
    assert saved["linux_systemd_catchup"]["verified"] is True
    assert not marker.exists()
    assert not handoff.pending_receipt_requires_catchup(latest, clover_home=tmp_path)


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_non_linux_receipt_boundary_skips_linux_intent(monkeypatch, tmp_path, platform):
    from clover_cli import config, update_receipt

    monkeypatch.setattr(config, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(update_receipt.sys, "platform", platform)
    monkeypatch.setattr(handoff, "inherit_pending_receipt", lambda *args, **kwargs: pytest.fail("non-Linux inheritance"))
    monkeypatch.setattr(update_receipt, "_current", None)
    update_receipt.begin_update_receipt()
    assert update_receipt._current is not None
    update_receipt._current.data["linux_systemd_catchup"] = {"verified": False}
    path = update_receipt.finalize_pending_update_receipt(0)
    assert path is not None
    assert json.loads(path.read_text())["outcome"] == "success"

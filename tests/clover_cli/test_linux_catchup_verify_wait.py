"""The Linux catch-up restart waits for the successor gateway before verifying.

``systemctl restart`` returns before the new gateway has rewritten its
``gateway_state.json`` (new PID + code_sha), so the first fleet probe still
reports the old PID. Verification must poll a bounded window instead of
failing on that first read. Real receipt/marker I/O in ``tmp_path``; only the
fleet probe, the restart itself and the clock are faked.
"""
from __future__ import annotations

import json
import types

import pytest

from clover_cli import linux_catchup_handoff as handoff

_SHA = "f" * 40
_OTHER_SHA = "e" * 40
_OLD_PID = 101
_NEW_PID = 301


def _row(pid, sha=_SHA):
    return [{"profile": "default", "pid": pid, "state": "current", "code_sha": sha}]


@pytest.fixture
def catchup(monkeypatch, tmp_path):
    """Arm a durable catch-up receipt; return (latest, marker, clock, run)."""
    from clover_cli import build_info, config, update_cmd, update_receipt

    monkeypatch.setattr(update_receipt.sys, "platform", "linux")
    receipt = tmp_path / "latest.json"
    receipt.write_text(json.dumps({
        "started_at": "2026-10-02T12:00:00+00:00", "pid": 10,
        "outcome": "running", "post_update": {},
        "linux_systemd_catchup": {
            "version": 1, "clover_home": str(tmp_path.resolve()),
            "expected_sha": _SHA, "targets": [{"profile": "default", "pid": _OLD_PID}],
        },
    }), encoding="utf-8")
    latest = tmp_path / "logs" / "update_receipts" / "latest.json"
    latest.parent.mkdir(parents=True)
    receipt.replace(latest)
    marker = tmp_path / "fleet_restart_pending"
    marker.write_bytes(b"expected_sha=" + _SHA.encode())

    monkeypatch.setattr(config, "get_clover_home", lambda: tmp_path)
    monkeypatch.setattr(handoff, "_supported", lambda: True)
    monkeypatch.setattr(build_info, "get_code_identity", lambda **kw: {"sha": _SHA})
    monkeypatch.setattr(update_receipt, "_current", None)
    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", lambda: True)

    class Clock:
        now = 0.0
        sleeps: list[float] = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    clock = Clock()
    clock.sleeps = []
    monkeypatch.setattr(update_cmd, "_time", types.SimpleNamespace(
        monotonic=clock.monotonic, sleep=clock.sleep, time=lambda: clock.now,
    ))

    polls: list = []

    def script_fleet(post_restart):
        """Post-restart probes return ``post_restart(n)`` for the nth call."""
        def collect(**kwargs):
            if kwargs.get("pre_restart_pids"):
                return _row(_OLD_PID)
            polls.append(1)
            return post_restart(len(polls))
        monkeypatch.setattr(update_receipt, "collect_fleet_versions", collect)

    update_receipt.begin_update_receipt()
    return types.SimpleNamespace(
        latest=latest, marker=marker, clock=clock, polls=polls,
        script_fleet=script_fleet, update_cmd=update_cmd,
        update_receipt=update_receipt,
    )


def test_successor_appearing_after_old_pid_polls_is_verified(catchup, tmp_path, capsys):
    catchup.script_fleet(lambda n: _row(_OLD_PID) if n <= 2 else _row(_NEW_PID))

    catchup.update_cmd._apply_linux_systemd_catchup({_OLD_PID})  # must not exit
    catchup.update_receipt.finalize_pending_update_receipt(0)

    assert len(catchup.polls) == 3
    saved = json.loads(catchup.latest.read_text())
    assert saved["outcome"] == "success"
    assert saved["linux_systemd_catchup"]["verified"] is True
    assert not catchup.marker.exists()
    assert not handoff.pending_receipt_requires_catchup(catchup.latest, clover_home=tmp_path)
    assert capsys.readouterr().out.count("Waiting for") == 1


def test_first_poll_success_does_not_wait(catchup):
    catchup.script_fleet(lambda n: _row(_NEW_PID))

    catchup.update_cmd._apply_linux_systemd_catchup({_OLD_PID})

    assert len(catchup.polls) == 1
    assert catchup.clock.sleeps == []


def test_fleet_that_never_converges_fails_after_bounded_wait(catchup, tmp_path):
    catchup.script_fleet(lambda n: _row(_OLD_PID))

    with pytest.raises(SystemExit) as exit_info:
        catchup.update_cmd._apply_linux_systemd_catchup({_OLD_PID})

    assert exit_info.value.code == 1
    assert 60 <= catchup.clock.now <= 100  # bounded: roughly the 90 s budget
    assert len(catchup.polls) > 3  # it did keep polling
    assert catchup.marker.exists()
    assert json.loads(catchup.latest.read_text())["outcome"] == "running"
    assert handoff.pending_receipt_requires_catchup(catchup.latest, clover_home=tmp_path)


def test_wrong_sha_after_restart_still_fails(catchup, tmp_path):
    catchup.script_fleet(lambda n: _row(_NEW_PID, sha=_OTHER_SHA))

    with pytest.raises(SystemExit) as exit_info:
        catchup.update_cmd._apply_linux_systemd_catchup({_OLD_PID})

    assert exit_info.value.code == 1
    assert catchup.marker.exists()
    assert json.loads(catchup.latest.read_text())["outcome"] == "running"
    assert handoff.pending_receipt_requires_catchup(catchup.latest, clover_home=tmp_path)

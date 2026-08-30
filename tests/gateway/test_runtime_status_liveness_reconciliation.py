"""Runtime-status liveness reconciliation: a dead gateway must never read "running".

Regression cover for the force-kill lie.  ``gateway_state.json`` is only ever
advanced by the gateway process itself, so a process killed WITHOUT its shutdown
handler running (``taskkill /F`` from the Windows auto-updater, SIGKILL, OOM,
power loss) leaves the file frozen on its last truthful-at-the-time claim.

The reported incident, verbatim: PID 21200, ``"gateway_state": "running"``,
``updated_at`` ten minutes old, and no such process on the box.  Any external
health check reading that file directly was misled.

The invariant under test: **a record whose PID is provably gone can never come
back from ``read_runtime_status()`` claiming a live state** — while a healthy
gateway, including a quiet idle one whose file is hours old, still reads running.
"""

import json
import os
from pathlib import Path

import pytest

from gateway import status


# --- helpers ---------------------------------------------------------------

# A pid/start_time pair standing in for the killed gateway. The exact numbers
# don't matter: _pid_exists is monkeypatched so the "is it alive" answer is
# deterministic and no real process is ever probed or signalled.
DEAD_PID = 21200
DEAD_START_TIME = 1111111


def _write_state(home: Path, **fields) -> Path:
    """Write a gateway_state.json by hand.

    Deliberately NOT via write_runtime_status(): that always stamps the CURRENT
    (live) process, which is precisely the case these tests must not exercise.
    """
    payload = {
        "pid": DEAD_PID,
        "kind": "clover-gateway",
        "start_time": DEAD_START_TIME,
        "gateway_state": "running",
        "active_agents": 0,
        "platforms": {},
        "updated_at": status._utc_now_iso(),
        "code_sha": "54c70d3a",
    }
    payload.update(fields)
    path = home / "gateway_state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _old_iso(seconds_ago: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def dead_pid(monkeypatch):
    """The recorded PID is gone from the process table."""
    monkeypatch.setattr(status, "_pid_exists", lambda pid: False)


@pytest.fixture
def live_pid(monkeypatch):
    """The recorded PID is alive AND is the same process (start_time matches)."""
    monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: DEAD_START_TIME)


class TestDeadPidCannotClaimRunning:
    """Positive proof of death must always beat the file's own claim."""

    def test_dead_pid_fresh_timestamp_does_not_read_running(self, home, dead_pid):
        """THE REPORTED INCIDENT: force-killed seconds ago.

        The record is well inside the freshness TTL, so every timestamp-based
        heuristic vouches for it — and it is still a lie. Only the process
        table can catch this one.
        """
        _write_state(home, gateway_state="running", updated_at=status._utc_now_iso())

        record = status.read_runtime_status()

        assert record is not None
        assert record["gateway_state"] != "running"
        assert record["gateway_state"] == "stopped"
        # Fresh by TTL, yet dead — proving the downgrade did NOT come from staleness.
        assert record["liveness"]["stale"] is False
        assert record["liveness"]["verdict"] == "dead"

    def test_dead_pid_old_timestamp_does_not_read_running(self, home, dead_pid):
        """Stale AND dead — the long-abandoned file. Same verdict."""
        _write_state(home, gateway_state="running", updated_at=_old_iso(6000))

        record = status.read_runtime_status()

        assert record["gateway_state"] == "stopped"
        assert record["liveness"]["stale"] is True
        assert record["liveness"]["verdict"] == "dead"

    def test_pid_reuse_does_not_read_running(self, home, monkeypatch):
        """PID number alive, but it's a DIFFERENT process.

        The OS recycled 21200 onto something unrelated. The start_time
        fingerprint is the only thing that can tell them apart, and the original
        gateway is just as dead as if the number were free.
        """
        monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
        monkeypatch.setattr(
            status, "_get_process_start_time", lambda pid: DEAD_START_TIME + 999
        )
        _write_state(home, gateway_state="running")

        record = status.read_runtime_status()

        assert record["gateway_state"] == "stopped"
        assert record["liveness"]["verdict"] == "reused"

    @pytest.mark.parametrize(
        "claimed", ["running", "starting", "draining", "stopping", "degraded"]
    )
    def test_every_live_asserting_state_is_downgraded(self, home, dead_pid, claimed):
        """"running" is not the only state that asserts a live process.

        A gateway killed mid-boot or mid-drain leaves "starting"/"draining"
        behind, which are equally false once the PID is gone.
        """
        _write_state(home, gateway_state=claimed)

        record = status.read_runtime_status()

        assert record["gateway_state"] == "stopped"
        assert record["gateway_state_reported"] == claimed


class TestHealthyGatewayIsNeverReportedDead:
    """The other side of the trade: no phantom outages."""

    def test_live_gateway_still_reads_running(self, home, live_pid):
        _write_state(home, gateway_state="running", updated_at=status._utc_now_iso())

        record = status.read_runtime_status()

        assert record["gateway_state"] == "running"
        # Untouched record: no reconciliation annotations bolted on.
        assert "liveness" not in record
        assert "gateway_state_reported" not in record

    def test_idle_gateway_with_stale_file_still_reads_running(self, home, live_pid):
        """CRITICAL anti-regression: old file + live PID = still running.

        A healthy but quiet gateway does not rewrite gateway_state.json, so its
        updated_at drifts arbitrarily far past the TTL. If staleness alone could
        drive the downgrade, every idle gateway on the fleet would be reported
        as a dead one. TTL is a hint; the process table is the proof.
        """
        _write_state(home, gateway_state="running", updated_at=_old_iso(86_400))

        record = status.read_runtime_status()

        assert status.runtime_status_is_stale(record) is True   # old, yes
        assert record["gateway_state"] == "running"             # dead, no
        assert "liveness" not in record

    def test_real_write_then_read_roundtrip_reads_running(self, home):
        """End-to-end with NO monkeypatching at all.

        write_runtime_status stamps this live pytest process, so the record
        describes something genuinely alive. Guards against the reconciliation
        misfiring on a real, unfaked live gateway.
        """
        status.write_runtime_status(gateway_state="running", active_agents=0)

        record = status.read_runtime_status()

        assert record["pid"] == os.getpid()
        assert record["gateway_state"] == "running"
        assert "liveness" not in record


class TestUnprovableClaimsAreLeftStanding:
    """"We could not tell" must never be rendered as "it is dead"."""

    def test_record_without_pid_is_not_downgraded(self, home, dead_pid):
        """No PID to check => absence of evidence, not evidence of absence."""
        _write_state(home, gateway_state="running", pid=None)

        record = status.read_runtime_status()

        assert record["gateway_state"] == "running"

    def test_probe_failure_degrades_to_raw_record(self, home, monkeypatch):
        """A raising process probe must not 500 a status endpoint."""

        def boom(pid):
            raise OSError("process table unavailable")

        monkeypatch.setattr(status, "_pid_exists", boom)
        _write_state(home, gateway_state="running")

        record = status.read_runtime_status()

        assert record is not None
        assert record["gateway_state"] == "running"

    def test_already_stopped_state_is_not_clobbered(self, home, dead_pid):
        """A clean shutdown already told the truth — leave it alone."""
        _write_state(home, gateway_state="stopped", exit_reason="clean")

        record = status.read_runtime_status()

        assert record["gateway_state"] == "stopped"
        assert record["exit_reason"] == "clean"
        assert "liveness" not in record

    def test_startup_failed_diagnostic_is_preserved(self, home, dead_pid):
        """"startup_failed" is real signal ("it died during boot") and is NOT a
        live claim. Rewriting it to "stopped" would erase why it never came up."""
        _write_state(home, gateway_state="startup_failed", exit_reason="port_in_use")

        record = status.read_runtime_status()

        assert record["gateway_state"] == "startup_failed"
        assert record["exit_reason"] == "port_in_use"


class TestAbsentOrUnreadableFile:
    def test_absent_file_returns_none(self, home, dead_pid):
        assert status.read_runtime_status() is None

    def test_corrupt_json_returns_none(self, home, dead_pid):
        (home / "gateway_state.json").write_text("{not json", encoding="utf-8")
        assert status.read_runtime_status() is None

    def test_empty_file_returns_none(self, home, dead_pid):
        (home / "gateway_state.json").write_text("", encoding="utf-8")
        assert status.read_runtime_status() is None


class TestForensicDataIsPreserved:
    """Reconciliation corrects the CLAIM without destroying the evidence."""

    def test_raw_claim_and_payload_survive_the_downgrade(self, home, dead_pid):
        _write_state(
            home,
            gateway_state="running",
            platforms={"telegram": {"state": "connected"}},
            code_sha="54c70d3a",
        )

        record = status.read_runtime_status()

        # The corrected verdict...
        assert record["gateway_state"] == "stopped"
        # ...alongside everything needed for the post-mortem.
        assert record["gateway_state_reported"] == "running"
        assert record["liveness"]["recorded_pid"] == DEAD_PID
        assert record["pid"] == DEAD_PID
        assert record["code_sha"] == "54c70d3a"
        assert record["platforms"] == {"telegram": {"state": "connected"}}

    def test_reconcile_false_returns_untouched_bytes(self, home, dead_pid):
        """Escape hatch for tooling that wants the literal file contents."""
        _write_state(home, gateway_state="running")

        record = status.read_runtime_status(reconcile=False)

        assert record["gateway_state"] == "running"
        assert "liveness" not in record

    def test_read_does_not_rewrite_the_file(self, home, dead_pid):
        """Reconcile-on-read, NOT self-heal-on-disk.

        A reader without write permission (dashboard enumerating another
        profile's home, a monitoring agent running as another user) must get the
        same correct answer, so the read path must not depend on writing.
        """
        path = _write_state(home, gateway_state="running")
        before = path.read_text(encoding="utf-8")

        assert status.read_runtime_status()["gateway_state"] == "stopped"

        assert path.read_text(encoding="utf-8") == before
        assert json.loads(before)["gateway_state"] == "running"

    def test_read_only_directory_still_reconciles(self, home, dead_pid):
        """The no-write-permission case, enforced by the filesystem."""
        path = _write_state(home, gateway_state="running")
        os.chmod(home, 0o500)  # r-x: reads fine, cannot create/replace
        try:
            record = status.read_runtime_status()
        finally:
            os.chmod(home, 0o700)

        assert record["gateway_state"] == "stopped"
        assert json.loads(path.read_text(encoding="utf-8"))["gateway_state"] == "running"

    def test_input_record_is_not_mutated_in_place(self, home, dead_pid):
        """Shared/cached dicts must not change underneath their owner."""
        original = {
            "pid": DEAD_PID,
            "start_time": DEAD_START_TIME,
            "gateway_state": "running",
            "updated_at": status._utc_now_iso(),
        }
        snapshot = dict(original)

        out = status._reconcile_runtime_status_liveness(original)

        assert out["gateway_state"] == "stopped"
        assert original == snapshot


class TestExistingContractsUnbroken:
    """The helpers this fix builds on must behave exactly as before."""

    def test_get_runtime_status_running_pid_still_rejects_dead_pid(self, home, dead_pid):
        _write_state(home, gateway_state="running")
        assert status.get_runtime_status_running_pid() is None

    def test_get_runtime_status_running_pid_accepts_live_self(self, home, monkeypatch):
        """A live record must still yield its PID after reconciliation.

        ``_record_matches_live_gateway_pid`` is stubbed because it inspects the
        process's argv for a gateway command line, and the pytest process is
        not one — it returns False here on unmodified main too, so asserting
        against it would test the runner, not this fix. Stubbing it isolates
        exactly the contract at risk: that reconciliation does not strip a
        genuine liveness answer out from under this function.
        """
        monkeypatch.setattr(
            status, "_record_matches_live_gateway_pid", lambda *a, **k: True
        )
        status.write_runtime_status(gateway_state="running")

        assert status.get_runtime_status_running_pid() == os.getpid()

    def test_runtime_status_pid_is_live_contract_preserved(self, home, live_pid):
        record = {"pid": DEAD_PID, "start_time": DEAD_START_TIME}
        assert status.runtime_status_pid_is_live(record) is True
        assert status.runtime_status_pid_is_live({"pid": None}) is False
        assert status.runtime_status_pid_is_live(None) is False

    def test_resolve_gateway_liveness_reports_down_for_dead_record(self, home, dead_pid):
        """The dashboard ladder must not be resurrected by the stale file."""
        _write_state(home, gateway_state="running")

        liveness = status.resolve_gateway_liveness(use_cache=False)

        assert liveness.running is False
        assert liveness.pid is None

    def test_resolve_gateway_liveness_still_reports_live_gateway_up(
        self, home, monkeypatch
    ):
        """Rung 3 (runtime_status) must still answer "up" for a live record.

        Same argv-stub reasoning as above; ``use_cache=False`` keeps the
        module-level PID cache out of it.
        """
        monkeypatch.setattr(
            status, "_record_matches_live_gateway_pid", lambda *a, **k: True
        )
        status.write_runtime_status(gateway_state="running")

        liveness = status.resolve_gateway_liveness(use_cache=False)

        assert liveness.running is True
        assert liveness.pid == os.getpid()


class TestConsumersInheritTheCorrection:
    """The whole point of fixing the chokepoint: consumers get it for free."""

    def test_derived_busy_and_drainable_are_false_for_dead_record(self, home, dead_pid):
        _write_state(home, gateway_state="running", active_agents=3)

        record = status.read_runtime_status()
        gw_state = record["gateway_state"]

        assert status.derive_gateway_busy(
            gateway_running=True, gateway_state=gw_state, active_agents=3
        ) is False
        assert status.derive_gateway_drainable(
            gateway_running=True, gateway_state=gw_state
        ) is False

    def test_health_snapshot_does_not_report_dead_gateway_as_running(
        self, home, dead_pid
    ):
        """The OTLP exporter path (agent/monitoring) reads the same record.

        This is the surface an external monitor actually alerts on, so it is
        where the original lie did real damage: a force-killed gateway kept
        exporting ``clover.gateway.busy=1`` with a "running" state label.
        """
        from agent.monitoring.gateway_health import build_gateway_health_snapshot

        _write_state(home, gateway_state="running", active_agents=2)
        runtime = status.read_runtime_status()

        snapshot = build_gateway_health_snapshot(
            runtime,
            gateway_running=True,
            profile="default",
            install_id="test",
            version="0.0.0",
        )

        state_labels = [
            m.attributes.get("clover.gateway.state")
            for m in snapshot.metrics
            if m.name == "clover.gateway.state"
        ]
        # "stopped" is existing known vocabulary, so it survives _bounded_state
        # and is exported as a real value rather than being dropped/coerced.
        assert state_labels == ["stopped"]
        assert "running" not in state_labels

        by_name = {m.name: m.value for m in snapshot.metrics}
        assert by_name["clover.gateway.busy"] == 0
        assert by_name["clover.gateway.drainable"] == 0

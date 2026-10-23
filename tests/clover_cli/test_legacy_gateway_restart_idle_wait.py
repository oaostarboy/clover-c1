"""A legacy (<= 1.5.6) gateway is restarted at an idle moment, never refused.

The released 1.5.6 updater trusts ``_graceful_restart_via_sigusr1`` from the NEW
``clover_cli/gateway.py``. The old gateway exits as soon as its agent count is
zero, even while a final reply is still being sent, and the successor then
replays that reply marked "may be a duplicate". These tests pin the bounded
quiet-moment wait that narrows that window, and its never-block-forever limit.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3

import pytest

from clover_cli import gateway

PID = 424242


class Clock:
    """Deterministic monotonic clock; sleeping advances it."""

    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept += seconds
        self.now += seconds


def _write_state(home, *, active=0, capability=None, pid=PID, **extra):
    row = {"pid": pid, "kind": "clover-gateway", "gateway_state": "running",
           "restart_requested": False, "active_agents": active,
           "clover_home": str(home), "start_time": 42, **extra}
    if capability is not None:
        row["update_restart_capability"] = capability
    (home / "gateway_state.json").write_text(json.dumps(row), encoding="utf-8")


def _ledger(home, rows):
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS delivery_obligations ("
        "obligation_id TEXT PRIMARY KEY, session_key TEXT, platform TEXT, chat_id TEXT, "
        "thread_id TEXT, content TEXT, state TEXT, attempts INTEGER DEFAULT 0, "
        "created_at REAL, updated_at REAL, owner_pid INTEGER, owner_started_at INTEGER, "
        "last_error TEXT, adapter_profile TEXT)")
    conn.execute("DELETE FROM delivery_obligations")
    for i, (state, owner) in enumerate(rows):
        conn.execute(
            "INSERT INTO delivery_obligations VALUES (?,?,?,?,?,?,?,0,0,0,?,0,NULL,NULL)",
            (f"o{i}", "s", "irc", "c", None, "x", state, owner))
    conn.commit()
    conn.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kw: [])
    import gateway.status as status
    monkeypatch.setattr(status, '_get_process_start_time', lambda pid: 42)
    monkeypatch.setattr(status, 'runtime_status_pid_is_live', lambda row: True)
    _ledger(tmp_path, [])
    return tmp_path


def test_capable_gateway_is_not_delayed(home):
    _write_state(home, active=3, capability="reply-carriers-through-ack-v1")
    clock = Clock()
    result = gateway.wait_for_legacy_gateway_idle(
        PID, max_wait=120, sleep=clock.sleep, clock=clock.monotonic)
    assert result == "not-legacy"
    assert clock.slept == 0


def test_unknown_pid_is_not_delayed(home):
    clock = Clock()
    assert gateway.wait_for_legacy_gateway_idle(
        PID + 1, max_wait=120, sleep=clock.sleep, clock=clock.monotonic) == "unverified"
    assert clock.slept == 0


def test_idle_legacy_gateway_restarts_after_short_stability_window(home):
    _write_state(home, active=0)
    _ledger(home, [("delivered", PID)])
    clock = Clock()
    result = gateway.wait_for_legacy_gateway_idle(
        PID, max_wait=120, poll=1, stable_seconds=3, sleep=clock.sleep, clock=clock.monotonic)
    assert result == "idle"
    assert 3 <= clock.slept <= 5


def test_waits_while_a_turn_is_active_then_proceeds_when_it_ends(home):
    _write_state(home, active=1)
    clock = Clock()
    calls = {"n": 0}
    real_sleep = clock.sleep

    def sleep(seconds):
        real_sleep(seconds)
        calls["n"] += 1
        if calls["n"] == 10:
            _write_state(home, active=0)

    result = gateway.wait_for_legacy_gateway_idle(
        PID, max_wait=120, poll=1, stable_seconds=3, sleep=sleep, clock=clock.monotonic)
    assert result == "idle"
    assert clock.slept >= 13


@pytest.mark.parametrize("state", ["pending", "attempting"])
def test_waits_for_in_flight_delivery_rows_of_that_gateway(home, state):
    _write_state(home, active=0)
    _ledger(home, [(state, PID)])
    clock = Clock()
    calls = {"n": 0}
    real_sleep = clock.sleep

    def sleep(seconds):
        real_sleep(seconds)
        calls["n"] += 1
        if calls["n"] == 7:
            _ledger(home, [("delivered", PID)])

    result = gateway.wait_for_legacy_gateway_idle(
        PID, max_wait=120, poll=1, stable_seconds=3, sleep=sleep, clock=clock.monotonic)
    assert result == "idle"
    assert clock.slept >= 10


def test_other_owners_rows_do_not_block(home):
    _write_state(home, active=0)
    _ledger(home, [("attempting", PID + 99), ("pending", None)])
    clock = Clock()
    assert gateway.wait_for_legacy_gateway_idle(
        PID, max_wait=120, poll=1, stable_seconds=3, sleep=clock.sleep,
        clock=clock.monotonic) == "idle"


def test_never_blocks_forever(home):
    _write_state(home, active=2)
    clock = Clock()
    result = gateway.wait_for_legacy_gateway_idle(
        PID, max_wait=30, poll=1, stable_seconds=3, sleep=clock.sleep, clock=clock.monotonic)
    assert result == "timeout"
    assert 30 <= clock.slept <= 32


def test_default_budget_is_bounded_between_one_and_three_minutes():
    assert 60 <= gateway.LEGACY_GATEWAY_IDLE_WAIT_SECONDS <= 180


def test_unreadable_ledger_defers_without_waiting(home):
    _write_state(home, active=0)
    (home / "state.db").write_bytes(b"not a database")
    clock = Clock()
    assert gateway.wait_for_legacy_gateway_idle(
        PID, max_wait=120, poll=1, stable_seconds=3, sleep=clock.sleep,
        clock=clock.monotonic) == "unverified"


def test_already_draining_gateway_is_not_delayed(home):
    _write_state(home, active=4, restart_requested=True, gateway_state="draining")
    clock = Clock()
    assert gateway.wait_for_legacy_gateway_idle(
        PID, max_wait=0, sleep=clock.sleep, clock=clock.monotonic) == "timeout"


def test_wait_does_not_modify_the_database(home):
    _write_state(home, active=0)
    _ledger(home, [("attempting", PID + 1)])
    before = (home / "state.db").read_bytes()
    clock = Clock()
    gateway.wait_for_legacy_gateway_idle(
        PID, max_wait=10, poll=1, stable_seconds=2, sleep=clock.sleep, clock=clock.monotonic)
    assert (home / "state.db").read_bytes() == before


def test_sigusr1_restart_waits_before_signalling_and_still_signals(home, monkeypatch):
    order = []
    monkeypatch.setattr(
        gateway, "wait_for_legacy_gateway_idle",
        lambda pid, **kw: order.append(("wait", pid)) or "idle")
    monkeypatch.setattr(os, "kill", lambda pid, sig: order.append(("kill", pid, sig)))
    monkeypatch.setattr(gateway, "_wait_for_pid_exit", lambda pid, timeout: True)
    assert gateway._graceful_restart_via_sigusr1(PID, 5.0) is True
    assert order == [("wait", PID), ("kill", PID, signal.SIGUSR1)]


def test_idle_wait_failure_never_blocks_the_restart(home, monkeypatch):
    def boom(pid, **kw):
        raise RuntimeError("probe exploded")

    killed = []
    monkeypatch.setattr(gateway, "wait_for_legacy_gateway_idle", boom)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(gateway, "_wait_for_pid_exit", lambda pid, timeout: True)
    with pytest.raises(SystemExit):
        gateway._graceful_restart_via_sigusr1(PID, 5.0)
    assert killed == []

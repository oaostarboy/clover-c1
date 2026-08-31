"""The restart watcher must be armed BEFORE the gateway is stopped.

Reported from a Windows install on 2026-08-31. Across four `clover update`
runs in under 24 hours the updater stopped the gateway and then died before
restarting it. The worst run left the bot offline for roughly nine hours
overnight.

A restart watcher exists and is correct, but ``_cmd_update_impl`` arms it
*after* ``_pause_windows_gateways_for_update()`` has already stopped the
gateway::

    _windows_gateway_resume = _m()._pause_windows_gateways_for_update()   # STOPPED here
    if _windows_gateway_resume:
        ...
        subprocess.Popen([... update_restart_watcher ...])                # ARMED here

Any kill between those two points, which is exactly what the operator
reported, leaves the gateway down with nothing watching. The watcher's own
docstring says it exists because "Python skips atexit on a signal", so the
window it does not cover is the window that matters.

These tests bind the ordering, not the implementation.
"""

from __future__ import annotations

import pytest


class TestWatcherArmedBeforeStop:
    """Ordering contract: arm the net before taking the gateway down."""

    def test_beacon_is_written_before_any_gateway_is_stopped(self, monkeypatch):
        """The beacon must exist by the time the pause routine runs.

        Simulates the reported kill: the updater dies inside the pause. The
        beacon must already be on disk, because that file is the only thing
        that lets an out-of-process watcher restore the gateway.
        """
        from clover_cli import update_cmd

        events: list[str] = []

        def fake_write_beacon(argv, *a, **kw):
            events.append("beacon_written")
            return "/tmp/fake-beacon"

        def fake_pause():
            events.append("gateway_stopped")
            raise KeyboardInterrupt("updater killed mid-pause")

        monkeypatch.setattr(
            "clover_cli.update_restart_watcher.write_beacon", fake_write_beacon
        )
        monkeypatch.setattr(update_cmd, "_pause_windows_gateways_for_update", fake_pause)
        # _m() indirects to clover_cli.main, which is the documented patch
        # surface for these helpers. The pause/restart path is Windows-only,
        # and so is the reported bug.
        monkeypatch.setattr("clover_cli.main._is_windows", lambda: True)
        monkeypatch.setattr(
            "clover_cli.main._gateway_restart_argv_for_running_gateway",
            lambda: ["clover", "gateway", "run"],
        )
        monkeypatch.setattr(
            "subprocess.Popen", lambda *a, **kw: type("P", (), {"pid": 1234})()
        )

        try:
            update_cmd._arm_restart_watcher_before_pause()
            update_cmd._pause_windows_gateways_for_update()
        except KeyboardInterrupt:
            pass

        assert "beacon_written" in events, (
            "no beacon was written before the gateway was stopped — "
            "a kill here leaves the gateway down with no recovery"
        )
        assert events.index("beacon_written") < events.index("gateway_stopped"), (
            f"watcher armed too late; order was {events}"
        )


class TestArmHelperExists:
    """The helper must be callable and must not raise on non-Windows."""

    def test_arm_helper_is_callable(self):
        from clover_cli import update_cmd

        assert callable(update_cmd._arm_restart_watcher_before_pause)

    def test_arm_failure_never_breaks_the_update(self, monkeypatch):
        """The watcher is a safety net; arming it must never abort an update."""
        from clover_cli import update_cmd

        def boom(*a, **kw):
            raise RuntimeError("no beacon dir")

        monkeypatch.setattr(
            "clover_cli.update_restart_watcher.write_beacon", boom
        )
        # Must swallow, exactly like the existing post-stop arming block.
        update_cmd._arm_restart_watcher_before_pause()


class TestArmingIsWiredIntoTheUpdateFlow:
    """The helper must actually be CALLED before the pause, not just exist.

    The behavioural test above drives the two calls by hand, so it would still
    pass if nobody wired the helper into the real update. This checks the
    wiring itself: in ``_cmd_update_impl`` the arming call must appear before
    the pause call. That ordering is the entire fix.
    """

    def _impl_source(self):
        import inspect

        from clover_cli import update_cmd

        return inspect.getsource(update_cmd._cmd_update_impl)

    def test_impl_arms_the_watcher(self):
        src = self._impl_source()
        assert "_arm_restart_watcher_before_pause()" in src, (
            "_cmd_update_impl never arms the pre-pause watcher — "
            "the gateway can be stopped with no recovery armed"
        )

    def test_impl_arms_before_it_pauses(self):
        src = self._impl_source()
        arm = src.index("_arm_restart_watcher_before_pause()")
        pause = src.index("_pause_windows_gateways_for_update()")
        assert arm < pause, (
            "the restart watcher is armed AFTER the gateway is stopped; "
            "a kill in that window leaves the gateway down with nothing "
            "watching — this is the reported nine-hour outage"
        )

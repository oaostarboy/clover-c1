"""``clover gateway run`` must report a clear line when a dependency import
fails, instead of dying with only a raw traceback.

``run_gateway()`` in clover_cli.gateway imports ``gateway.run`` lazily right
before starting. If a package the gateway module chain depends on is
missing (e.g. after a partial dependency install), that import raises
ModuleNotFoundError/ImportError uncaught, which under a systemd
Restart=on-failure unit just crash-loops with nothing but a traceback in
the log. This must instead print one clear line naming the missing
package and the fix command, then exit cleanly.
"""

from __future__ import annotations

import types

import pytest


def _prepare(monkeypatch):
    import clover_cli.gateway as gateway_cli

    monkeypatch.setattr(gateway_cli, "_guard_official_docker_root_gateway", lambda: None)
    monkeypatch.setattr(gateway_cli, "_guard_named_profile_under_multiplexer", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_guard_supervised_gateway_conflict", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_guard_existing_gateway_process_conflict", lambda replace=False: None)
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    return gateway_cli


def test_run_gateway_reports_missing_dependency_instead_of_raw_traceback(
    monkeypatch, capsys
):
    gateway_cli = _prepare(monkeypatch)

    def _broken_import():
        raise ModuleNotFoundError("No module named 'aiohttp'", name="aiohttp")

    monkeypatch.setattr(gateway_cli, "_import_start_gateway", _broken_import)

    with pytest.raises(SystemExit) as excinfo:
        gateway_cli.run_gateway()

    assert excinfo.value.code == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "aiohttp" in combined
    assert "clover update" in combined


def test_run_gateway_still_starts_normally_when_import_succeeds(monkeypatch):
    """Regression guard: the try/except around the import must not swallow
    a healthy import path. Full success-path coverage (asyncio.run +
    start_gateway + the hard-exit backstop) already lives in
    tests/clover_cli/test_gateway_run_hard_exit.py.
    """
    gateway_cli = _prepare(monkeypatch)
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_cli, "_import_start_gateway", lambda: gateway_run.start_gateway)

    async def _start_gateway(*args, **kwargs):
        return True

    def _fake_run(coro):
        coro.close()
        return True

    def _hard_exit(code: int) -> None:
        raise SystemExit(code)

    monkeypatch.setattr(gateway_run, "start_gateway", _start_gateway)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", _hard_exit)
    monkeypatch.setattr(gateway_cli.asyncio, "run", _fake_run)

    with pytest.raises(SystemExit) as excinfo:
        gateway_cli.run_gateway()

    assert excinfo.value.code == 0

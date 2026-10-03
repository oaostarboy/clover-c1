"""Linux systemd catch-up result handoff contracts."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import gateway.run as gateway_run
from clover_cli import linux_catchup_handoff as handoff
from gateway.config import Platform
from tests.gateway.restart_test_helpers import make_restart_runner


def _receipt(home, *, targets, sha="f" * 40, started=None, outcome="running"):
    path = home / "logs" / "update_receipts" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": 1, "started_at": started or datetime.now(timezone.utc).isoformat(),
        "outcome": outcome, "post_update": {},
        "linux_systemd_catchup": {
            "version": 1, "clover_home": str(home), "expected_sha": sha,
            "targets": targets,
        },
    }), encoding="utf-8")
    return path


async def _conclude(monkeypatch, home, fleet):
    monkeypatch.setattr(gateway_run, "_clover_home", home)
    monkeypatch.setattr(gateway_run.sys, "platform", "linux")
    from clover_cli import update_receipt
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", lambda **kw: fleet)
    (home / ".update_pending.claimed.json").write_text("{}", encoding="utf-8")
    runner, adapter = make_restart_runner()
    paths = [home / n for n in (".update_pending.json", ".update_pending.claimed.json",
                                ".update_output.txt", ".update_exit_code",
                                ".update_prompt.json")]
    await runner._conclude_update_after_updater_death(
        *paths, adapter=adapter, chat_id="42", session_key=None,
        metadata=None, platform=Platform.TELEGRAM,
    )
    return adapter


@pytest.mark.asyncio
async def test_catchup_receipt_is_success_only_after_every_profile_restarts_on_expected_sha(
    monkeypatch, tmp_path,
):
    _receipt(tmp_path, targets=[{"profile": "default", "pid": 101},
                                {"profile": "work", "pid": 202}])
    adapter = await _conclude(monkeypatch, tmp_path, [
        {"profile": "default", "pid": 301, "code_sha": "f" * 40, "state": "current"},
        {"profile": "work", "pid": 402, "code_sha": "f" * 40, "state": "current"},
    ])
    record = json.loads((tmp_path / "logs/update_receipts/latest.json").read_text())
    assert record["outcome"] == "success"
    assert not (tmp_path / "fleet_restart_pending").exists()
    assert "finished" in adapter.sent[0]


@pytest.mark.parametrize("fleet", [
    [{"profile": "default", "pid": 301, "code_sha": "e" * 40, "state": "stale"}],
    [{"profile": "default", "pid": 101, "code_sha": "f" * 40, "state": "current"}],
    [],
    [{"profile": "default", "pid": 301, "code_sha": None, "state": "down"}],
])
@pytest.mark.asyncio
async def test_unverified_catchup_preserves_restart_obligation(monkeypatch, tmp_path, fleet):
    _receipt(tmp_path, targets=[{"profile": "default", "pid": 101}])
    marker = tmp_path / "fleet_restart_pending"
    marker.write_text("expected_sha=" + "f" * 40)
    adapter = await _conclude(monkeypatch, tmp_path, fleet)
    record = json.loads((tmp_path / "logs/update_receipts/latest.json").read_text())
    assert record["outcome"] != "success"
    assert marker.exists()
    assert "finished" not in adapter.sent[0]


@pytest.mark.asyncio
async def test_stale_catchup_receipt_cannot_claim_current_run(monkeypatch, tmp_path):
    _receipt(tmp_path, targets=[{"profile": "default", "pid": 101}],
             started="2020-01-01T00:00:00+00:00")
    adapter = await _conclude(monkeypatch, tmp_path, [
        {"profile": "default", "pid": 301, "code_sha": "f" * 40, "state": "current"},
    ])
    assert "finished" not in adapter.sent[0]


@pytest.mark.parametrize("platform", ["win32", "darwin"])
@pytest.mark.asyncio
async def test_non_linux_does_not_run_catchup_verification(monkeypatch, tmp_path, platform):
    _receipt(tmp_path, targets=[{"profile": "default", "pid": 101}])
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(gateway_run.sys, "platform", platform)
    from clover_cli import update_receipt
    monkeypatch.setattr(update_receipt, "collect_fleet_versions",
                        lambda **kw: pytest.fail("cross-platform catch-up verification"))
    (tmp_path / ".update_pending.claimed.json").write_text("{}")
    runner, adapter = make_restart_runner()
    paths = [tmp_path / n for n in (".update_pending.json", ".update_pending.claimed.json",
                                    ".update_output.txt", ".update_exit_code",
                                    ".update_prompt.json")]
    await runner._conclude_update_after_updater_death(
        *paths, adapter=adapter, chat_id="42", session_key=None,
        metadata=None, platform=Platform.TELEGRAM,
    )


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_non_linux_producer_guard_precedes_new_imports(monkeypatch, platform):
    import builtins
    import sys

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"clover_cli.gateway", "clover_cli.build_info"}:
            pytest.fail("non-Linux producer imported catch-up dependencies")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert handoff.prepare_pending_receipt(
        object(), clover_home=object(), pre_restart_pids=set()
    ) is False


def test_producer_tags_only_joined_owned_systemd_pids(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from clover_cli import build_info

    monkeypatch.setattr(handoff, "_supported", lambda: True)
    monkeypatch.setattr(build_info, "get_code_identity", lambda **kw: {"sha": "a" * 40})
    saved = []
    api = SimpleNamespace(
        _current=SimpleNamespace(data={}),
        collect_fleet_versions=lambda **kw: [
            {"profile": "default", "pid": 101, "state": "current"},
            {"profile": "foreign", "pid": 999, "state": "current"},
        ],
        save_pending_receipt=lambda *args: saved.append(args),
    )

    assert handoff.prepare_pending_receipt(
        api, clover_home=tmp_path, pre_restart_pids={101}
    )
    tag = api._current.data["linux_systemd_catchup"]
    assert tag["targets"] == [{"profile": "default", "pid": 101}]
    assert tag["clover_home"] == str(tmp_path.resolve())
    assert saved == [("a" * 40, None)]


def test_producer_refuses_pids_without_current_install_profile_match(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from clover_cli import build_info

    monkeypatch.setattr(handoff, "_supported", lambda: True)
    monkeypatch.setattr(build_info, "get_code_identity", lambda **kw: {"sha": "a" * 40})
    api = SimpleNamespace(
        _current=SimpleNamespace(data={}),
        collect_fleet_versions=lambda **kw: [
            {"profile": "foreign", "pid": 999, "state": "current"}
        ],
        save_pending_receipt=lambda *args: pytest.fail("unjoined PID persisted"),
    )
    assert not handoff.prepare_pending_receipt(
        api, clover_home=tmp_path, pre_restart_pids={101}
    )


@pytest.mark.parametrize("platform", ["win32", "darwin"])
@pytest.mark.asyncio
async def test_tagged_success_uses_legacy_verdict_on_non_linux(monkeypatch, tmp_path, platform):
    _receipt(tmp_path, targets=[{"profile": "default", "pid": 101}], outcome="success")
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(gateway_run.sys, "platform", platform)
    from clover_cli import update_receipt
    monkeypatch.setattr(update_receipt, "collect_fleet_versions",
                        lambda **kw: pytest.fail("cross-platform catch-up verification"))
    (tmp_path / ".update_pending.claimed.json").write_text("{}")
    runner, adapter = make_restart_runner()
    paths = [tmp_path / n for n in (".update_pending.json", ".update_pending.claimed.json",
                                    ".update_output.txt", ".update_exit_code",
                                    ".update_prompt.json")]
    await runner._conclude_update_after_updater_death(
        *paths, adapter=adapter, chat_id="42", session_key=None,
        metadata=None, platform=Platform.TELEGRAM,
    )
    assert "finished" in adapter.sent[0]


@pytest.mark.parametrize("outcome", ["failed", "partial", "rolled-back"])
@pytest.mark.asyncio
async def test_catchup_verification_never_overwrites_terminal_failure(monkeypatch, tmp_path, outcome):
    receipt_path = _receipt(
        tmp_path, targets=[{"profile": "default", "pid": 101}], outcome=outcome
    )
    before = receipt_path.read_text(encoding="utf-8")
    adapter = await _conclude(monkeypatch, tmp_path, [
        {"profile": "default", "pid": 301, "code_sha": "f" * 40, "state": "current"},
    ])
    assert receipt_path.read_text(encoding="utf-8") == before
    assert "finished" not in adapter.sent[0]


def test_no_systemd_service_keeps_original_linux_catchup_path(monkeypatch):
    from clover_cli import update_cmd
    import clover_cli.gateway as clover_gateway

    monkeypatch.setattr(update_cmd, "_pending_fleet_restart_needed", lambda: True)
    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", lambda: True)
    monkeypatch.setattr(update_cmd, "_clear_fleet_restart_pending_marker", lambda: None)
    monkeypatch.setattr(clover_gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(update_cmd, "_owned_systemd_service_pids",
                        lambda: pytest.fail("no-service Linux handoff probe"))
    update_cmd._apply_pending_fleet_restart_catchup()

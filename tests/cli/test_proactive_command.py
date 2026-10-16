"""Real CLI dispatch and config-reader controls in a disposable profile."""
from unittest.mock import MagicMock
import copy

import pytest
import yaml

from agent.workstreams import load_settings
from clover_cli.config import read_raw_config


@pytest.fixture
def control(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    from cli import CloverCLI
    cli = CloverCLI.__new__(CloverCLI)
    cli.config = {}
    cli.console = MagicMock()
    cli.agent = None
    cli.session_id = "test-proactive"
    cli.conversation_history = []
    return cli, home


def test_cli_toggle_uses_bounded_preset_and_live_settings_reader(control):
    cli, home = control
    assert cli.process_command("/proactive") is True
    text = str(cli.console.print.call_args)
    assert "Off" in text and "/proactive on" in text
    assert not (home / "config.yaml").exists()
    assert cli.process_command("/proactive on") is True
    actual = load_settings()
    assert actual.active and actual.recall_enabled and actual.compaction_audit_enabled
    assert not actual.suggestions_enabled and not actual.procedures_enabled
    assert actual.compaction_audit_max_repairs == 0
    assert actual.private_only and not actual.eligible_jobs
    assert actual.aux_max_calls_per_day == 4
    assert "next turn" in str(cli.console.print.call_args)
    assert not (home / "cron" / "jobs.json").exists()
    assert not (home / "cron" / "notepad.db").exists()
    saved = (home / "config.yaml").read_bytes()
    cli.process_command("/proactive on")
    assert (home / "config.yaml").read_bytes() == saved
    cli.process_command("/proactive off")
    assert not load_settings().enabled
    assert read_raw_config()["proactive"]["mode"] == "active"
    assert cli.conversation_history == [] and cli.agent is None


def test_active_preferences_records_jobs_and_existing_loop_survive_toggles(control, monkeypatch):
    from clover_cli import config, loops, goals
    from cron import jobs, notepad
    from agent.workstreams import Scope
    cli, home = control
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", None)
    goals._DB_CACHE.clear()
    preferences = {
        "enabled": True, "mode": "active", "private_only": False,
        "eligible_jobs": ["existing-approved"],
        "threads": {"max_items": 5, "max_context_bytes": 1234, "retention_days": 8},
        "recall": {"enabled": False, "max_items": 2, "max_bytes": 256},
        "compaction_audit": {"enabled": False, "max_repairs": 0},
        "suggestions": {"enabled": False, "preferred_timing": "never", "cooldown_seconds": 900, "max_per_session": 0},
        "procedures": {"enabled": False, "min_verified_runs": 9, "min_sessions": 5},
        "aux": {"max_calls_per_event": 0, "max_calls_per_day": 0, "max_output_tokens": 128},
    }
    config.save_config({"proactive": preferences, "auxiliary": {"vision": {"model": "saved-model", "max_tokens": 91}}}, strip_defaults=False)
    scope = Scope(str(home.resolve()), "test", "private", "local")
    row = notepad.create_workstream(scope, kind="obligation", title="pending", summary="preserve me", fingerprint="pending")
    job = jobs.create_job("existing schedule", "every 1h", deliver="local")
    loop = loops.LoopManager("test-proactive")
    loops.dispatch_loop_command(loop, "5m existing loop")
    before_loop = loops.load_loop("test-proactive").to_json()
    before_db = (home / "cron" / "notepad.db").read_bytes()
    before_jobs = (home / "cron" / "jobs.json").read_bytes()
    before_config = config.read_raw_config()
    for action in ["off", "off", "on", "on", "status"]:
        cli.process_command("/proactive " + action)
        assert (home / "cron" / "notepad.db").read_bytes() == before_db
        assert (home / "cron" / "jobs.json").read_bytes() == before_jobs
        assert loops.load_loop("test-proactive").to_json() == before_loop
    assert config.read_raw_config() == before_config
    assert notepad.get_workstream(scope, row["workstream_id"]) == row
    assert jobs.get_job(job["id"])["enabled"]
    goals._DB_CACHE.clear()


@pytest.mark.parametrize("failure", ["exception", "silent", "malformed"])
def test_write_failure_never_announces_success(control, monkeypatch, failure):
    from clover_cli import config
    cli, home = control
    if failure == "exception":
        def fail(*args, **kwargs):
            raise OSError("read only")
        monkeypatch.setattr(config, "save_config", fail)
    elif failure == "silent":
        monkeypatch.setattr(config, "save_config", lambda *a, **k: None)
    else:
        (home / "config.yaml").write_text("proactive: [broken")
    cli.process_command("/proactive on")
    text = str(cli.console.print.call_args)
    assert "could not be confirmed" in text and "Saved" not in text
    assert not load_settings().enabled


def test_status_shows_persisted_and_managed_effective_state(control, tmp_path, monkeypatch):
    from clover_cli import managed_scope
    cli, home = control
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("proactive:\n  enabled: true\n  mode: active\n")
    monkeypatch.setenv("CLOVER_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        cli.process_command("/proactive status")
        text = str(cli.console.print.call_args)
        assert "Proactive: Off (saved" in text
        assert "Effective settings: On (mode: active)" in text
        assert not (home / "config.yaml").exists()
    finally:
        managed_scope.invalidate_managed_cache()


@pytest.mark.parametrize("timing", ["after_reply", "never"])
def test_first_enable_preserves_timing_limits_and_aux_budgets(control, timing):
    from clover_cli import config
    cli, _ = control
    config.save_config({"proactive": {
        "mode": "shadow", "enabled": False, "private_only": True,
        "eligible_jobs": ["owned-monitor"],
        "recall": {"enabled": False, "max_bytes": 128},
        "suggestions": {"enabled": False, "preferred_timing": timing, "cooldown_seconds": 7200},
        "aux": {"max_calls_per_event": 0, "max_calls_per_day": 0, "max_output_tokens": 64}
    }}, strip_defaults=False)
    cli.process_command("/proactive on")
    saved = config.read_raw_config()["proactive"]
    assert saved["suggestions"] == {"enabled": False, "preferred_timing": timing, "cooldown_seconds": 7200}
    assert saved["eligible_jobs"] == ["owned-monitor"]
    assert saved["recall"] == {"enabled": True, "max_bytes": 128}
    assert saved["compaction_audit"] == {"enabled": True}
    assert saved["aux"] == {"max_calls_per_event": 0, "max_calls_per_day": 0, "max_output_tokens": 64}
    assert load_settings().active


def test_status_does_not_migrate_an_older_config(control):
    cli, home = control
    path = home / "config.yaml"
    path.write_text("_config_version: 12\nproactive:\n  enabled: false\n")
    before = path.read_bytes()
    cli.process_command("/proactive status")
    assert "Off" in str(cli.console.print.call_args)
    assert path.read_bytes() == before


def test_malformed_status_reports_unknown_without_backup_write(control):
    cli, home = control
    path = home / "config.yaml"
    path.write_text("proactive: [broken")
    cli.process_command("/proactive status")
    assert "could not be confirmed" in str(cli.console.print.call_args)
    assert not list(home.glob("config.yaml.corrupt.*.bak"))
    assert path.read_text() == "proactive: [broken"


@pytest.fixture
def managed_preferences(control, tmp_path, monkeypatch):
    """Real saved user values that differ from administrator overlay values."""
    from clover_cli import config, managed_scope

    cli, home = control
    config.save_config({
        "proactive": {
            "enabled": True, "mode": "active", "private_only": True,
            "recall": {"enabled": False, "max_bytes": 128},
            "suggestions": {"preferred_timing": "never", "enabled": False},
            "aux": {"max_calls_per_event": 0, "max_calls_per_day": 0},
        },
        "privacy": {"redact_pii": True},
        "auxiliary": {"vision": {"model": "saved-user-model", "max_tokens": 91}},
        "custom_preferences": {"nested": ["keep", {"explicit": False}]},
    }, strip_defaults=False, strip_managed=False)
    # Establish a current-schema persisted baseline before testing toggles;
    # load_settings() uses the normal migrating reader at operation boundaries.
    config.load_config()
    managed = tmp_path / "managed"
    managed.mkdir()
    overlay = {
        "privacy": {"redact_pii": True},
        "auxiliary": {"vision": {"max_tokens": 45}},
    }
    (managed / "config.yaml").write_text(yaml.safe_dump(overlay))
    monkeypatch.setenv("CLOVER_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        yield cli, home, managed, overlay
    finally:
        managed_scope.invalidate_managed_cache()


@pytest.mark.parametrize("action", ["on", "off"])
@pytest.mark.parametrize("unchanged_pin", [False, True])
def test_managed_overlay_toggle_preserves_whole_raw_document(managed_preferences, monkeypatch, action, unchanged_pin):
    from clover_cli import config, managed_scope

    cli, home, managed, overlay = managed_preferences
    if unchanged_pin:
        # An unchanged proactive pin must not block a master/preset toggle.
        overlay["proactive"] = {"private_only": True}
        (managed / "config.yaml").write_text(yaml.safe_dump(overlay))
        managed_scope.invalidate_managed_cache()
    if action == "on":
        raw = config.read_raw_config()
        raw["proactive"].update(enabled=False, mode="shadow")
        config.save_config(raw, strip_defaults=False, strip_managed=False)
    before = config.read_raw_config()
    expected = copy.deepcopy(before)
    expected["proactive"]["enabled"] = action == "on"
    if action == "on":
        expected["proactive"].update(mode="active")
        expected["proactive"]["recall"]["enabled"] = True
        expected["proactive"]["compaction_audit"] = {"enabled": True}
    cli.process_command("/proactive " + action)
    assert "Saved;" in str(cli.console.print.call_args)
    assert config.read_raw_config() == expected
    effective = config.load_config(migrate=False)
    assert effective["privacy"]["redact_pii"] is True
    assert effective["auxiliary"]["vision"]["max_tokens"] == 45
    assert load_settings().enabled == (action == "on")
    saved_bytes = (home / "config.yaml").read_bytes()
    cli.process_command("/proactive " + action)
    assert "Already saved" in str(cli.console.print.call_args)
    assert (home / "config.yaml").read_bytes() == saved_bytes
    monkeypatch.delenv("CLOVER_MANAGED_DIR")
    managed_scope.invalidate_managed_cache()
    effective = config.load_config(migrate=False)
    assert effective["privacy"]["redact_pii"] is True
    assert effective["auxiliary"]["vision"]["max_tokens"] == 91
    assert config.read_raw_config() == expected


@pytest.mark.parametrize("pin", [
    {"enabled": True},
    {"mode": "shadow"},
    {"recall": {"enabled": False}},
    {"compaction_audit": {"enabled": False}},
    {"recall": False},
])
def test_managed_proactive_modified_pins_reject_without_document_write(managed_preferences, pin):
    from clover_cli import config, managed_scope

    cli, home, managed, overlay = managed_preferences
    raw = config.read_raw_config()
    raw["proactive"].update(enabled=False, mode="shadow")
    config.save_config(raw, strip_defaults=False, strip_managed=False)
    overlay["proactive"] = pin
    (managed / "config.yaml").write_text(yaml.safe_dump(overlay))
    managed_scope.invalidate_managed_cache()
    before = (home / "config.yaml").read_bytes()
    cli.process_command("/proactive on")
    text = str(cli.console.print.call_args)
    assert "could not be confirmed" in text and "Saved;" not in text
    assert (home / "config.yaml").read_bytes() == before
    effective = config.load_config(migrate=False)["proactive"]
    for key, value in pin.items():
        if isinstance(value, dict):
            for child, child_value in value.items():
                assert effective[key][child] == child_value
        else:
            assert effective[key] == value


def test_managed_proactive_disable_pin_rejects_without_document_write(managed_preferences):
    from clover_cli import managed_scope

    cli, home, managed, overlay = managed_preferences
    overlay["proactive"] = {"enabled": True}
    (managed / "config.yaml").write_text(yaml.safe_dump(overlay))
    managed_scope.invalidate_managed_cache()
    before = (home / "config.yaml").read_bytes()
    cli.process_command("/proactive off")
    assert "could not be confirmed" in str(cli.console.print.call_args)
    assert (home / "config.yaml").read_bytes() == before
    assert load_settings().enabled is True


@pytest.mark.parametrize("action", ["on", "off"])
@pytest.mark.parametrize("failure", ["atomic", "silent", "managed_install"])
def test_managed_overlay_failed_write_preserves_entire_document(managed_preferences, monkeypatch, action, failure):
    from clover_cli import config
    import utils

    cli, home, _, _ = managed_preferences
    raw = config.read_raw_config()
    raw["proactive"]["enabled"] = action != "on"
    config.save_config(raw, strip_defaults=False, strip_managed=False)
    before = (home / "config.yaml").read_bytes()
    if failure == "atomic":
        def fail(*args, **kwargs):
            raise OSError("synthetic atomic writer failure")
        monkeypatch.setattr(utils, "atomic_yaml_write", fail)
    elif failure == "silent":
        monkeypatch.setattr(config, "save_config", lambda *a, **k: None)
    else:
        monkeypatch.setattr(config, "is_managed", lambda: True)
    cli.process_command("/proactive " + action)
    text = str(cli.console.print.call_args)
    assert "could not be confirmed" in text and "Saved;" not in text
    assert (home / "config.yaml").read_bytes() == before

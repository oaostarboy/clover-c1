"""Real raw-file controls, with no live profile or provider access."""
from clover_cli import config
from clover_cli.commands import resolve_command
from clover_cli.proactive import proactive_command
from tests.gateway.test_proactive_command import home  # noqa: F401


def test_fresh_on_selects_disclosed_useful_package_and_off_on_preserves_it(home):
    menu = proactive_command(can_change=True)
    assert "suggestions" in menu and "Customize" in menu
    assert not (home / "config.yaml").exists()
    result = proactive_command("on", can_change=True)
    raw = config.require_readable_config_before_write()["proactive"]
    assert raw["suggestions"] == {"enabled": True, "preferred_timing": "after_reply",
                                  "cooldown_seconds": 3600, "max_per_session": 1}
    assert raw["recall"]["enabled"] and raw["compaction_audit"]["enabled"]
    assert not raw.get("procedures", {}).get("enabled", False)
    assert "suggestions" in result and "3600" in result and "1/session" in result
    assert "aux" not in raw and "eligible_jobs" not in raw
    assert "Saved" in proactive_command("off", can_change=True)
    assert "Saved" in proactive_command("on", can_change=True)
    assert config.require_readable_config_before_write()["proactive"] == raw


def test_customize_draft_cancel_save_and_stale_conflict(home):
    from clover_cli.proactive import ProactiveDraft
    draft = ProactiveDraft.open()
    draft.set("recall", "on")
    assert "Recall: On" in draft.render("recall")
    assert not (home / "config.yaml").exists()  # discard = Cancel, no write
    assert "Saved" in draft.save()
    saved = config.require_readable_config_before_write()
    assert saved["proactive"]["recall"]["enabled"] is True
    assert not saved["proactive"].get("enabled", False)
    stale = ProactiveDraft.open()
    stale.set("audit", "on")
    assert "Saved" in proactive_command("recall off", can_change=True)
    before = (home / "config.yaml").read_bytes()
    assert "changed" in stale.save().lower()
    assert (home / "config.yaml").read_bytes() == before


def test_invalid_control_section_refuses_on_before_any_write(home):
    config.save_config({'proactive': {'enabled': False, 'suggestions': False}}, strip_defaults=False)
    before = (home / 'config.yaml').read_bytes()
    assert 'could not be confirmed' in proactive_command('on', can_change=True)
    assert (home / "config.yaml").read_bytes() == before


def test_customize_shows_managed_master_and_mode_difference_without_writing(home, tmp_path, monkeypatch):
    from clover_cli import managed_scope
    from clover_cli.proactive import ProactiveDraft
    managed = tmp_path / 'managed-customize'
    managed.mkdir()
    (managed / 'config.yaml').write_text('proactive:\n  enabled: true\n  mode: active\n')
    monkeypatch.setenv('CLOVER_MANAGED_DIR', str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        assert 'managed policy' in ProactiveDraft.open().render()
        assert not (home / 'config.yaml').exists()
    finally:
        managed_scope.invalidate_managed_cache()


def test_advanced_inventory_and_paid_watcher_are_discoverable_but_never_activate(home):
    assert {"customize", "advanced", "watcher"} <= set(resolve_command("proactive").subcommands)
    for action, phrase in [("advanced", "Task / job monitors"), ("watcher", "extra model usage"),
                           ("monitors", "unchanged"), ("judges", "not wired"),
                           ("repairs", "not wired"), ("scope", "private"),
                           ("routines", "scheduled jobs"), ("desktop", "not available")]:
        text = proactive_command(action, can_change=True)
        assert phrase in text, (action, text)
        assert not (home / "config.yaml").exists()
    for action in ["watcher on", "watcher accept", "judges on", "repairs on", "desktop on"]:
        text = proactive_command(action, can_change=True)
        assert "nothing changed" in text
        assert not (home / "config.yaml").exists()


def test_invalid_stored_timing_is_not_reflected_into_public_controls(home):
    config.save_config({"proactive": {"suggestions": {"preferred_timing": "PRIVATE-CREDENTIAL-CANARY"}}},
                       strip_defaults=False)
    before = (home / "config.yaml").read_bytes()
    for action in ["status", "customize"]:
        assert "PRIVATE-CREDENTIAL-CANARY" not in proactive_command(action, can_change=True)
    assert (home / "config.yaml").read_bytes() == before



def test_text_choices_have_parity_and_invalid_or_denied_inputs_do_not_write(home):
    from clover_cli.proactive import ProactiveDraft
    for action in ['recall on', 'suggestions on', 'timing never', 'frequency daily', 'audit on', 'skill-drafts on']:
        assert 'Saved' in proactive_command(action, can_change=True)
    raw = config.require_readable_config_before_write()['proactive']
    assert raw['recall']['enabled'] and raw['procedures']['enabled']
    assert raw['compaction_audit']['enabled']
    assert raw['suggestions'] == {'enabled': True, 'preferred_timing': 'never',
                                  'cooldown_seconds': 86400, 'max_per_session': 1}
    before = (home / 'config.yaml').read_bytes()
    for action in ['recall yes', 'suggestions true', 'frequency 0', 'timing now', 'audit on extra',
                   '5m check CI', 'cancel', 'save', 'watcher accept']:
        assert 'nothing changed' in proactive_command(action, can_change=True)
        assert (home / 'config.yaml').read_bytes() == before
    for action in ['on', 'off', 'recommended', 'recall off', 'suggestions off', 'timing after_reply',
                   'frequency hourly', 'audit off', 'skill-drafts off']:
        assert 'locked' in proactive_command(action, can_change=False)
        assert (home / 'config.yaml').read_bytes() == before
    draft = ProactiveDraft.open()
    draft.set('recall', 'off')
    latest = config.require_readable_config_before_write()
    latest['independent'] = {'do-not-stomp': True}
    config.save_config(latest, strip_defaults=False, strip_managed=False)
    assert 'Saved' in draft.save()
    assert config.require_readable_config_before_write()['independent'] == {'do-not-stomp': True}


def test_managed_customization_rejects_all_leaf_changes_atomically(home, tmp_path, monkeypatch):
    from clover_cli import managed_scope
    from clover_cli.proactive import ProactiveDraft
    config.save_config({'proactive': {'mode': 'active', 'suggestions': {'enabled': False}}}, strip_defaults=False)
    managed = tmp_path / 'managed-leaf'
    managed.mkdir()
    (managed / 'config.yaml').write_text('proactive:\n  suggestions:\n    enabled: false\n')
    monkeypatch.setenv('CLOVER_MANAGED_DIR', str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        draft = ProactiveDraft.open()
        draft.recommended()
        before = (home / 'config.yaml').read_bytes()
        assert 'Not saved' in draft.save()
        assert (home / 'config.yaml').read_bytes() == before
    finally:
        managed_scope.invalidate_managed_cache()

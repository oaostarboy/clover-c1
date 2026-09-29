"""v41: example-shipped global display keys stop shadowing platform defaults.

Windows 11 report (Francis, 2026-09-29): the per-turn "🛠 N tool calls" summary
card never collapsed on Telegram. Telegram defaults ``cleanup_progress`` ON,
but the example config set ``display.cleanup_progress: false`` at the GLOBAL
level, the installers copy that example into config.yaml, and a global value
outranks the platform default. Owner acceptance: a config created from
69a75bb's example must resolve Telegram cleanup_progress to True after
/update with no manual step.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from clover_cli.config import get_config_path, migrate_config, read_raw_config
from gateway.display_config import resolve_display_setting

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cli-config-69a75bb.yaml.example"


def _install_69a75bb_config() -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")


def test_69a75bb_example_config_gets_the_telegram_summary_card_after_update():
    _install_69a75bb_config()
    before = yaml.safe_load(get_config_path().read_text(encoding="utf-8"))
    assert resolve_display_setting(before, "telegram", "cleanup_progress") is False

    migrate_config(interactive=False, quiet=True)  # what `clover update` runs

    cfg = read_raw_config()
    assert resolve_display_setting(cfg, "telegram", "cleanup_progress") is True
    assert resolve_display_setting(cfg, "discord", "cleanup_progress") is True
    assert resolve_display_setting(cfg, "slack", "cleanup_progress") is True
    # The other example-shipped shadows are gone too: platform defaults apply.
    assert resolve_display_setting(cfg, "signal", "tool_progress") == "off"
    assert resolve_display_setting(cfg, "telegram", "busy_ack_detail") is False
    # Platforms without their own default still get the old global value.
    assert resolve_display_setting(cfg, "api_server", "tool_progress") == before["display"]["tool_progress"]


def test_gateway_startup_probe_alone_applies_it():
    """The first gateway start after /update migrates even if update didn't."""
    from clover_cli.config import load_config

    _install_69a75bb_config()
    load_config()
    cfg = read_raw_config()
    assert resolve_display_setting(cfg, "telegram", "cleanup_progress") is True


def test_explicit_telegram_choice_is_left_alone():
    from clover_cli.config_migrations import _v41_rewrite_config

    cfg = {"display": {"cleanup_progress": False,
                       "platforms": {"telegram": {"cleanup_progress": False}}}}
    notes = _v41_rewrite_config(cfg)
    assert "cleanup_progress" in cfg["display"]
    assert all("cleanup_progress" not in n for n in notes)
    assert resolve_display_setting(cfg, "telegram", "cleanup_progress") is False


def test_user_values_that_differ_from_the_shipped_example_are_kept():
    from clover_cli.config_migrations import _v41_rewrite_config

    cfg = {"display": {"cleanup_progress": True, "tool_progress": "new",
                       "busy_ack_detail": "true", "platforms": {"slack": {"tool_progress": "all"}}}}
    assert _v41_rewrite_config(cfg) == []
    assert cfg["display"]["tool_progress"] == "new"
    assert cfg["display"]["platforms"] == {"slack": {"tool_progress": "all"}}


def test_second_run_changes_nothing():
    from clover_cli.config_migrations import _v41_rewrite_config

    cfg = {"display": {"cleanup_progress": False, "tool_progress": "all"}}
    assert len(_v41_rewrite_config(cfg)) == 2
    assert _v41_rewrite_config(cfg) == []
    assert cfg["display"] == {}


def test_shipped_example_no_longer_shadows_platform_defaults():
    from gateway.display_config import _PLATFORM_DEFAULTS

    example = Path(__file__).resolve().parents[2] / "cli-config.yaml.example"
    display = yaml.safe_load(example.read_text(encoding="utf-8"))["display"]
    shadowing = [
        key for key, value in display.items()
        if not isinstance(value, dict) and key != "streaming"
        and any(key in d and d[key] != value for d in _PLATFORM_DEFAULTS.values())
    ]
    assert shadowing == []

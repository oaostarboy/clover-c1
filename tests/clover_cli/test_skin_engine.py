"""Tests for clover_cli.skin_engine — the data-driven skin/theme system."""

import pytest


@pytest.fixture(autouse=True)
def reset_skin_state():
    """Reset skin engine state between tests."""
    from clover_cli import skin_engine
    skin_engine._active_skin = None
    skin_engine._active_skin_name = "boring"
    yield
    skin_engine._active_skin = None
    skin_engine._active_skin_name = "boring"


class TestSkinConfig:
    def test_default_skin_has_required_fields(self):
        from clover_cli.skin_engine import load_skin
        skin = load_skin("boring")
        assert skin.name == "boring"
        assert skin.tool_prefix == "┊"
        assert "banner_title" in skin.colors
        assert "banner_border" in skin.colors
        assert "agent_name" in skin.branding


    def test_get_spinner_wings_empty_for_default(self):
        from clover_cli.skin_engine import load_skin
        skin = load_skin("boring")
        assert skin.get_spinner_wings() == []


class TestBuiltinSkins:
    def test_ares_skin_loads(self):
        from clover_cli.skin_engine import load_skin
        skin = load_skin("ares")
        assert skin.name == "ares"
        assert skin.tool_prefix == "╎"
        # Crimson identity: border stays red-dominant (exact values are owned
        # by the palette audit in test_skin_palettes.py, which enforces
        # contrast floors — don't pin literals here).
        border = skin.get_color("banner_border")
        r, g, b = (int(border[i:i + 2], 16) for i in (1, 3, 5))
        assert r > g and r > b, f"ares border lost its crimson: {border}"
        assert skin.get_color("response_border") == "#C7A96B"
        assert skin.get_color("session_label") == "#C7A96B"
        assert skin.get_color("session_border") == "#6E584B"
        assert skin.get_branding("agent_name") == "Ares Agent"

    def test_ares_has_spinner_customization(self):
        from clover_cli.skin_engine import load_skin
        skin = load_skin("ares")
        wings = skin.get_spinner_wings()
        assert len(wings) > 0
        assert isinstance(wings[0], tuple)
        assert len(wings[0]) == 2








class TestSkinManagement:
    def test_set_active_skin(self):
        from clover_cli.skin_engine import set_active_skin, get_active_skin, get_active_skin_name
        skin = set_active_skin("ares")
        assert skin.name == "ares"
        assert get_active_skin_name() == "ares"
        assert get_active_skin().name == "ares"


    def test_list_skins_includes_builtins(self):
        from clover_cli.skin_engine import list_skins
        skins = list_skins()
        names = [s["name"] for s in skins]
        assert "default" in names
        assert "boring" in names
        assert "ares" in names
        assert "mono" in names
        assert "slate" in names
        assert "daylight" in names
        assert "warm-lightmode" in names
        for s in skins:
            assert "source" in s
            assert s["source"] == "builtin"




class TestUserSkins:
    def test_load_user_skin_from_yaml(self, tmp_path, monkeypatch):
        from clover_cli.skin_engine import load_skin
        # Create a user skin YAML
        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        skin_file = skins_dir / "custom.yaml"
        skin_data = {
            "name": "custom",
            "description": "A custom test skin",
            "colors": {"banner_title": "#FF0000"},
            "branding": {"agent_name": "Custom Agent"},
            "tool_prefix": "▸",
        }
        import yaml
        skin_file.write_text(yaml.dump(skin_data))

        # Patch skins dir
        monkeypatch.setattr("clover_cli.skin_engine._skins_dir", lambda: skins_dir)

        skin = load_skin("custom")
        assert skin.name == "custom"
        assert skin.get_color("banner_title") == "#FF0000"
        assert skin.get_branding("agent_name") == "Custom Agent"
        assert skin.tool_prefix == "▸"
        # Should inherit defaults for unspecified colors
        assert skin.get_color("banner_border") == "#2E8B57"  # from default

    def test_load_user_skin_invalid_section_types_fall_back_to_defaults(self, tmp_path, monkeypatch):
        from clover_cli.skin_engine import load_skin

        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        import yaml

        (skins_dir / "broken.yaml").write_text(
            yaml.dump(
                {
                    "name": "broken",
                    "colors": ["not", "a", "mapping"],
                    "spinner": "invalid",
                    "branding": ["also", "invalid"],
                    "tool_emojis": ["invalid"],
                    "tool_prefix": "!",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("clover_cli.skin_engine._skins_dir", lambda: skins_dir)

        skin = load_skin("broken")

        assert skin.name == "broken"
        assert skin.get_color("banner_title") == "#00FF87"
        assert skin.get_branding("agent_name") == "Clover Cognition"
        assert skin.spinner.get("waiting_faces", []) == []
        assert skin.tool_emojis == {}
        assert skin.tool_prefix == "!"

    def test_list_skins_includes_user_skins(self, tmp_path, monkeypatch):
        from clover_cli.skin_engine import list_skins
        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        import yaml
        (skins_dir / "pirate.yaml").write_text(yaml.dump({
            "name": "pirate",
            "description": "Arr matey",
        }))
        monkeypatch.setattr("clover_cli.skin_engine._skins_dir", lambda: skins_dir)

        skins = list_skins()
        names = [s["name"] for s in skins]
        assert "pirate" in names
        pirate = [s for s in skins if s["name"] == "pirate"][0]
        assert pirate["source"] == "user"


class TestDisplayIntegration:


    def test_tool_message_uses_skin_prefix(self):
        from clover_cli.skin_engine import set_active_skin
        from agent.display import get_cute_tool_message
        set_active_skin("ares")
        msg = get_cute_tool_message("terminal", {"command": "ls"}, 0.5)
        assert msg.startswith("╎")
        assert "┊" not in msg


class TestCliBrandingHelpers:


    def test_active_goodbye_ares(self):
        from clover_cli.skin_engine import set_active_skin, get_active_goodbye

        set_active_skin("ares")
        assert get_active_goodbye() == "Farewell, warrior! ⚔"

    def test_prompt_toolkit_style_overrides_cover_tui_classes(self):
        from clover_cli.skin_engine import set_active_skin, get_prompt_toolkit_style_overrides
        set_active_skin("ares")
        overrides = get_prompt_toolkit_style_overrides()
        required = {
            "input-area",
            "placeholder",
            "prompt",
            "prompt-working",
            "hint",
            "status-bar",
            "status-bar-strong",
            "status-bar-dim",
            "status-bar-good",
            "status-bar-warn",
            "status-bar-bad",
            "status-bar-critical",
            "input-rule",
            "image-badge",
            "completion-menu",
            "completion-menu.completion",
            "completion-menu.completion.current",
            "completion-menu.meta.completion",
            "completion-menu.meta.completion.current",
            "status-bar",
            "status-bar-strong",
            "status-bar-dim",
            "status-bar-good",
            "status-bar-warn",
            "status-bar-bad",
            "status-bar-critical",
            "voice-status",
            "voice-status-recording",
            "clarify-border",
            "clarify-title",
            "clarify-question",
            "clarify-choice",
            "clarify-selected",
            "clarify-active-other",
            "clarify-countdown",
            "sudo-prompt",
            "sudo-border",
            "sudo-title",
            "sudo-text",
            "approval-border",
            "approval-title",
            "approval-desc",
            "approval-cmd",
            "approval-choice",
            "approval-selected",
        }
        assert required.issubset(overrides.keys())

    def test_prompt_toolkit_style_overrides_use_skin_colors(self):
        from clover_cli.skin_engine import (
            set_active_skin,
            get_active_skin,
            get_prompt_toolkit_style_overrides,
        )

        set_active_skin("ares")
        skin = get_active_skin()
        overrides = get_prompt_toolkit_style_overrides()
        assert overrides["prompt"] == skin.get_color("prompt")
        assert overrides["input-rule"] == skin.get_color("input_rule")
        assert overrides["status-bar"] == (
            f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('status_bar_text')}"
        )
        assert overrides["status-bar-strong"] == (
            f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('status_bar_strong')} bold"
        )
        assert overrides["status-bar-critical"] == (
            f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('status_bar_critical')} bold"
        )
        assert overrides["clarify-title"] == f"{skin.get_color('banner_title')} bold"
        assert overrides["sudo-prompt"] == f"{skin.get_color('ui_error')} bold"
        assert overrides["approval-title"] == f"{skin.get_color('ui_warn')} bold"

        set_active_skin("daylight")
        skin = get_active_skin()
        overrides = get_prompt_toolkit_style_overrides()
        assert overrides["status-bar"] == f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('banner_text')}"
        assert overrides["voice-status"] == f"bg:{skin.get_color('voice_status_bg')} {skin.get_color('ui_label')}"


class TestCloIsDefaultInvariants:
    """Clo is the built-in ``default``; the plain look is ``boring``; ``clover`` is a hidden alias."""

    def _clo(self, skin):
        return skin.spinner.get("flavor") == "clover" and skin.spinner.get("done_mark") == "🍀"

    @pytest.mark.parametrize("name", ["default", "clover", "Clover", " clover "])
    def test_clo_loads_under_default_and_the_clover_alias(self, name):
        from clover_cli.skin_engine import init_skin_from_config, get_active_skin, get_active_skin_name

        init_skin_from_config({"display": {"skin": name}})
        skin = get_active_skin()
        assert skin.name == "default"
        assert get_active_skin_name() == "default"
        assert self._clo(skin)
        assert skin.spinner["fail_mark"] == "🥀"

    def test_alias_loads_without_a_warning(self, caplog):
        import logging
        from clover_cli.skin_engine import load_skin

        with caplog.at_level(logging.WARNING, logger="clover_cli.skin_engine"):
            load_skin("clover")
        assert not [r for r in caplog.records if "not found" in r.getMessage()]

    def test_boring_renders_the_classic_marks(self):
        from clover_cli.skin_engine import load_skin

        skin = load_skin("boring")
        assert skin.name == "boring"
        assert not self._clo(skin)
        assert skin.spinner.get("flavor") is None
        assert not skin.spinner.get("done_mark")

    def test_boring_card_marks_are_classic(self, monkeypatch):
        from clover_cli.skin_engine import set_active_skin
        from agent.display import get_done_mark, get_fail_mark

        set_active_skin("boring")
        assert (get_done_mark(), get_fail_mark()) == ("✅", "❌")
        set_active_skin("clover")
        assert (get_done_mark(), get_fail_mark()) == ("🍀", "🥀")

    def test_picker_lists_default_and_boring_but_not_the_alias(self, tmp_path, monkeypatch):
        from clover_cli import skin_engine
        from clover_cli.skin_cmd import resolve_skin_arg

        monkeypatch.setattr(skin_engine, "_skins_dir", lambda: tmp_path / "none")
        names = [s["name"] for s in skin_engine.list_skins()]
        assert names[:2] == ["default", "boring"]
        assert "clover" not in names
        # ...yet typing the old name still resolves to Clo.
        assert resolve_skin_arg("clover") == "default"
        assert resolve_skin_arg("boring") == "boring"

    def test_user_skin_named_clover_is_not_hijacked_by_the_alias(self, tmp_path, monkeypatch):
        import yaml
        from clover_cli import skin_engine

        d = tmp_path / "skins"
        d.mkdir()
        (d / "clover.yaml").write_text(yaml.safe_dump({"name": "clover", "colors": {"banner_title": "#123456"}}))
        monkeypatch.setattr(skin_engine, "_skins_dir", lambda: d)
        assert skin_engine.load_skin("clover").colors["banner_title"] == "#123456"

    def test_missing_keys_still_inherit_the_plain_base(self):
        from clover_cli.skin_engine import _build_skin_config, _BUILTIN_SKINS

        skin = _build_skin_config({"name": "bare"})
        assert skin.colors == _BUILTIN_SKINS["boring"]["colors"]
        assert skin.tool_prefix == "┊"
        assert not self._clo(skin)

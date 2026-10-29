"""Tests for agent naming and config detection during onboarding."""

from unittest.mock import patch
import pytest

from clover_cli.setup import setup_agent_name, SETUP_SECTIONS


def test_setup_sections_includes_agent_name():
    section_keys = [key for key, _, _ in SETUP_SECTIONS]
    assert "name" in section_keys
    name_entry = next(entry for entry in SETUP_SECTIONS if entry[0] == "name")
    assert name_entry[1] == "Agent Name"
    assert name_entry[2] == setup_agent_name


def test_setup_agent_name_persists_to_branding_and_agent(tmp_path, monkeypatch):
    monkeypatch.setattr("clover_cli.config.get_clover_home", lambda: tmp_path)
    monkeypatch.setattr("clover_cli.setup.prompt", lambda prompt_text, default="": "Jarvis")

    saved_config = {}
    monkeypatch.setattr("clover_cli.setup.save_config", lambda cfg: saved_config.update(cfg))

    config = {}
    setup_agent_name(config)

    assert config["branding"]["agent_name"] == "Jarvis"
    assert config["agent"]["name"] == "Jarvis"
    assert saved_config["branding"]["agent_name"] == "Jarvis"
    assert saved_config["agent"]["name"] == "Jarvis"


def test_setup_agent_name_defaults_to_clover_on_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("clover_cli.config.get_clover_home", lambda: tmp_path)
    monkeypatch.setattr("clover_cli.setup.prompt", lambda prompt_text, default="": "")
    monkeypatch.setattr("clover_cli.setup.save_config", lambda cfg: None)

    config = {}
    setup_agent_name(config)

    assert config["branding"]["agent_name"] == "Clover"
    assert config["agent"]["name"] == "Clover"

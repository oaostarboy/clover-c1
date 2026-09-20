"""Behavior contract for the optional adversarial AI council skill."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "optional-skills" / "autonomous-ai-agents" / "council"
RUNNER_PATH = SKILL_DIR / "scripts" / "council_run.py"
ROSTER_PATH = SKILL_DIR / "references" / "models.json"


def _load_runner():
    spec = importlib.util.spec_from_file_location("council_run", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_registers_as_council_slash_command():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["name"] == "council"
    assert len(frontmatter["description"]) <= 60
    assert "/council" in text


def test_harness_scanner_exposes_council_slash_command(tmp_path, monkeypatch):
    from agent import skill_commands, skill_utils
    from tools import skills_tool

    skills_dir = tmp_path / "skills"
    installed = skills_dir / "autonomous-ai-agents" / "council"
    shutil.copytree(SKILL_DIR, installed)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_utils, "get_external_skills_dirs", lambda *a, **k: [])
    monkeypatch.setattr(skill_utils, "get_project_skills_dirs", lambda *a, **k: [])
    monkeypatch.setattr(skill_commands, "_skill_commands", {})
    monkeypatch.setattr(skill_commands, "_skill_commands_platform", None)
    monkeypatch.setattr(skill_commands, "_skill_commands_home", None)

    commands = skill_commands.scan_skill_commands()

    assert commands["/council"]["name"] == "council"
    assert skill_commands.resolve_skill_command_key("council") == "/council"


def test_roster_matches_ant_and_octavias_evidence_backed_assignments():
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    assert roster == {
        "STEELMAN": {"provider": "anthropic", "model": "claude-fable-5-1"},
        "PROSECUTOR": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        "PREMISE": {"provider": "gemini-oauth", "model": "gemini-3.8-flash-high"},
        "PRAGMATIST": {"provider": "gemini-oauth", "model": "gemini-3.8-flash-high"},
        "OUTSIDER": {"provider": "xai-oauth", "model": "grok-4.6"},
        "HISTORIAN": {"provider": "xai-oauth", "model": "grok-4.6"},
        "CHAIRMAN": {"provider": "anthropic", "model": "claude-opus-5"},
        "ATTACK": {"provider": "openai-codex", "model": "gpt-6-astra"},
    }


def test_modes_preserve_octavias_council_shape():
    runner = _load_runner()
    assert runner.MODE_SEATS["quick"] == ["STEELMAN", "PROSECUTOR", "PRAGMATIST"]
    assert runner.MODE_SEATS["full"] == [
        "STEELMAN", "PROSECUTOR", "PREMISE", "PRAGMATIST", "OUTSIDER"
    ]
    assert runner.MODE_SEATS["deep"] == [
        "STEELMAN", "PROSECUTOR", "PREMISE", "PRAGMATIST", "OUTSIDER", "HISTORIAN"
    ]


def test_route_command_uses_exact_provider_and_model(tmp_path):
    runner = _load_runner()
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    cmd = runner.build_seat_command(
        "PROSECUTOR", "do the task", roster, clover_bin="/opt/clover"
    )
    assert cmd == [
        "/opt/clover", "-z", "do the task", "-m", "gpt-5.6-sol",
        "--provider", "openai-codex",
    ]


def test_missing_seat_is_a_hard_error_not_silent_model_fallback():
    runner = _load_runner()
    with pytest.raises(KeyError, match="CHAIRMAN"):
        runner.build_seat_command("CHAIRMAN", "decide", {}, clover_bin="clover")


def test_usage_route_accepts_verified_gemini_provider_alias(tmp_path):
    runner = _load_runner()
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({
        "completed": True,
        "provider": "custom",
        "model": "gemini-3.8-flash-high",
    }))
    roster = {
        "PRAGMATIST": {
            "provider": "gemini-oauth",
            "model": "gemini-3.8-flash-high",
        }
    }
    assert runner.validate_usage_route("PRAGMATIST", roster, usage) is None


def test_usage_route_rejects_silent_model_substitution(tmp_path):
    runner = _load_runner()
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({
        "completed": True,
        "provider": "anthropic",
        "model": "claude-opus-5",
    }))
    roster = {
        "PROSECUTOR": {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        }
    }
    with pytest.raises(RuntimeError, match="PROSECUTOR.*requested.*gpt-5.6-sol.*answered.*claude-opus-5"):
        runner.validate_usage_route("PROSECUTOR", roster, usage)


def test_unparseable_attack_fails_toward_extra_review():
    runner = _load_runner()
    assert runner.parse_severity("no severity line") == "SERIOUS"
    assert runner.parse_severity("SEVERITY: FATAL") == "FATAL"
    assert runner.parse_severity("SEVERITY: nonsense") == "SERIOUS"


def test_chairman_verdict_parser_accepts_plain_or_markdown_labels():
    runner = _load_runner()
    text = "**VERDICT:** build the smaller version\nNEXT: run one pilot\nDISSENT: scale may suffer"
    assert runner.parse_verdict(text) == (
        "build the smaller version", "run one pilot", "scale may suffer"
    )


def test_historian_prompt_uses_active_clover_home(tmp_path):
    runner = _load_runner()
    prompt = runner.seat_task(
        "HISTORIAN", "Should we do it?", tmp_path / "answer.md", clover_home=tmp_path
    )
    assert str(tmp_path / "MEMORY.md") in prompt
    assert str(tmp_path / "memories") in prompt
    assert ".openclaw" not in prompt


def test_report_exposes_final_verdict_for_the_calling_agent(tmp_path):
    runner = _load_runner()
    report = tmp_path / "run.report.md"
    runner.write_final_report(
        report,
        question="Should we ship?",
        mode="quick",
        verdict="ship the pilot",
        next_step="deploy to ten users",
        dissent="small sample",
        chairman_text="VERDICT: ship the pilot",
        elapsed_s=42,
        returned=3,
        expected=3,
        reviews=0,
    )
    text = report.read_text(encoding="utf-8")
    assert "VERDICT: ship the pilot" in text
    assert "NEXT: deploy to ten users" in text
    assert "DISSENT: small sample" in text
    assert "42s" in text

"""Opt-in compact skill discovery integration tests (synthetic fixtures)."""

from pathlib import Path

from clover_constants import set_clover_home_override, reset_clover_home_override
from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache


def _skill(root: Path, category: str, name: str, description: str) -> None:
    path = root / category / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n")


def _catalog(home: Path, compact_config: str | None) -> str:
    skills = home / "skills"
    _skill(skills, "openclaw-imports", "import-one", "Imported routing detail")
    _skill(skills, "openclaw-imports", "import-two", "Another imported detail")
    _skill(skills, "autonomous-ai-agents", "core-ops", "Core operations description")
    (home / "config.yaml").write_text(
        "skills:\n  compact_categories:" + (f"\n    - {compact_config}" if compact_config else " []") + "\n"
    )
    token = set_clover_home_override(str(home))
    try:
        clear_skills_system_prompt_cache()
        return build_skills_system_prompt(skills_dir_override=skills)
    finally:
        reset_clover_home_override(token)


def test_config_compacts_only_named_category_and_preserves_every_skill_name(tmp_path, monkeypatch):
    prompt = _catalog(tmp_path, "openclaw-imports")

    assert "openclaw-imports [names only]: import-one, import-two" in prompt
    assert "Imported routing detail" not in prompt
    assert "autonomous-ai-agents:" in prompt
    assert "Core operations description" in prompt
    assert "import-one" in prompt and "import-two" in prompt and "core-ops" in prompt
    assert "load with skill_view(name) as usual" in prompt
    from tools import skills_tool
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path / "skills")
    assert "body" in skills_tool.skill_view("import-one")
    assert "body" in skills_tool.skill_view("import-two")
    assert "body" in skills_tool.skill_view("core-ops")


def test_invalid_config_shape_is_ignored_and_explicit_categories_still_work(tmp_path):
    skills = tmp_path / "skills"
    _skill(skills, "openclaw-imports", "import-one", "Imported routing detail")
    (tmp_path / "config.yaml").write_text("skills:\n  compact_categories: true\n")
    token = set_clover_home_override(str(tmp_path))
    try:
        clear_skills_system_prompt_cache()
        full = build_skills_system_prompt(skills_dir_override=skills)
        compact = build_skills_system_prompt(
            compact_categories=frozenset({"openclaw-imports"}), skills_dir_override=skills
        )
        assert "Imported routing detail" in full
        assert "openclaw-imports [names only]: import-one" in compact
    finally:
        reset_clover_home_override(token)


def test_catalog_build_is_deterministic_for_resume_and_does_not_mutate_existing_prompt(tmp_path):
    first = _catalog(tmp_path, "openclaw-imports")
    # A resumed conversation retains its already-built system prompt bytes.
    resumed_prompt = first
    second = _catalog(tmp_path, "openclaw-imports")

    assert second == first
    assert resumed_prompt == first

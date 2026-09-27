"""Gateway-only installs must reseed a missing/deleted bundled skills/ dir.

Before this fix, only ``clover chat`` (clover_cli/main.py's cmd_chat) checked
for and repaired an unseeded ``skills/`` directory on startup. A gateway-only
install (``clover gateway run`` with no prior CLI launch, or a skills/ dir
deleted after install) never ran that check, so bundled skills silently
never landed.
"""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_gateway_start_reseeds_missing_skills_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))

    skills_dir = tmp_path / "skills"
    assert not skills_dir.exists()

    config = GatewayConfig(platforms={}, sessions_dir=tmp_path / "sessions")
    runner = GatewayRunner(config)

    await runner.start()

    assert skills_dir.is_dir(), "gateway startup must reseed the missing skills/ dir"
    assert next(skills_dir.rglob("SKILL.md"), None) is not None


@pytest.mark.asyncio
async def test_gateway_start_leaves_populated_skills_dir_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))

    skills_dir = tmp_path / "skills"
    skill_md = skills_dir / "my-custom-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: my-custom-skill\n---\nCustom.\n", encoding="utf-8")

    config = GatewayConfig(platforms={}, sessions_dir=tmp_path / "sessions")
    runner = GatewayRunner(config)

    await runner.start()

    assert skill_md.exists(), "an already-populated skills/ dir must be left as-is"

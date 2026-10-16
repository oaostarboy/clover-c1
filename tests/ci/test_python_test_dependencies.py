"""CI must provision the real Telegram SDK before hermetic wire tests run."""

import shlex
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("workflow", "job"),
    [("tests.yml", "test"), ("tests.yml", "e2e"), ("tests-os.yml", "os-tests")],
)
def test_python_test_job_selects_locked_telegram_dependency(workflow, job):
    config = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    step = next(
        step for step in config["jobs"][job]["steps"]
        if step.get("name") == "Install dependencies"
    )
    command = shlex.split(step["with"]["command"])
    assert command[:2] == ["uv", "sync"]
    assert "--locked" in command
    extras = [command[i + 1] for i, arg in enumerate(command) if arg == "--extra"]
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = metadata["project"]["optional-dependencies"]
    # [all]/[dev] deliberately omit lazy messaging dependencies. Check the
    # selected dependency contract, not a particular extra's spelling.
    selected = {
        Requirement(req).name
        for extra in extras
        for req in dependencies[extra]
    }
    assert "python-telegram-bot" in selected, (
        f"{workflow}:{job} must install PTB before lazy installs are disabled"
    )


def test_telegram_keyboard_uses_installed_sdk_serialization():
    from importlib.metadata import version

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    assert version("python-telegram-bot")
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Enable", callback_data="proactive:on")],
        [InlineKeyboardButton("Disable", callback_data="proactive:off")],
    ])
    assert markup.to_dict() == {
        "inline_keyboard": [
            [{"text": "Enable", "callback_data": "proactive:on"}],
            [{"text": "Disable", "callback_data": "proactive:off"}],
        ],
    }

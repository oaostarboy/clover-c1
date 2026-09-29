"""thinking_progress is on by default in gateway chats.

The owner wants the model's own short notes between tool calls ("💭 *…*") to
show live on every agent without per-agent config. The resolver lives inside
the gateway turn setup, so assert on the source contract: the default passed
for ``thinking_progress`` is True, while Mattermost keeps its per-platform
opt-in and an explicit false still wins.
"""

import ast
from pathlib import Path

RUN = Path(__file__).resolve().parents[2] / "gateway" / "run.py"


def _surface_mode_calls():
    tree = ast.parse(RUN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "_display_surface_mode"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            yield node.args[0].value, {k.arg: k.value for k in node.keywords}


def test_thinking_progress_defaults_on():
    calls = {name: kw for name, kw in _surface_mode_calls()}
    assert "thinking_progress" in calls
    default = calls["thinking_progress"].get("default")
    assert isinstance(default, ast.Constant) and default.value is True


def test_thinking_progress_keeps_mattermost_opt_in():
    calls = {name: kw for name, kw in _surface_mode_calls()}
    assert "require_platform_override_for" in calls["thinking_progress"]


def test_explicit_false_still_disables():
    from gateway.display_config import resolve_display_setting

    cfg = {"display": {"platforms": {"telegram": {"thinking_progress": False}}}}
    assert resolve_display_setting(cfg, "telegram", "thinking_progress", True) is False

"""Bug C: subagents must never install into a live agent's venv."""

import os
from pathlib import Path

import pytest

from tools import live_venv_guard as g


@pytest.fixture
def venvs(tmp_path):
    live = tmp_path / "agents" / "clover-c1" / "venv"
    (live / "bin").mkdir(parents=True)
    other = tmp_path / "agents" / "tentacle-one" / "venv"
    (other / "bin").mkdir(parents=True)
    return [str(live.resolve()), str(other.resolve())]


@pytest.mark.parametrize(
    "cmd_tpl",
    [
        # The 10:11 shape: VIRTUAL_ENV inherited/pointed at the live venv.
        "cd /wt && VIRTUAL_ENV={live} uv pip install -e .",
        "export VIRTUAL_ENV={live}; uv pip install -e '.[all,dev]'",
        "source {live}/bin/activate && uv pip install -e .",
        "uv pip install --python {live}/bin/python -e .",
        "uv sync --python {live}/bin/python --locked",
        "UV_PROJECT_ENVIRONMENT={live} uv sync --locked",
        "{live}/bin/python -m pip install -e .",
        "{live}/bin/pip install requests",
        "{other}/bin/python -m pip install -e /wt",
        # Bare pip in a child shell resolves to the live venv on PATH.
        "pip install -e .",
        "python -m pip install -e .",
    ],
)
def test_blocks_installs_into_live_venvs(venvs, cmd_tpl):
    cmd = cmd_tpl.format(live=venvs[0], other=venvs[1])
    assert g.detect_live_venv_install(cmd, venvs), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "uv venv .venv && uv pip install --python .venv/bin/python -e .",
        "VIRTUAL_ENV=/tmp/v311 uv pip install -e '.[all,dev]'",
        "source .venv/bin/activate && pip install -e .",
        "uv sync --locked",  # project-local .venv
        "pip list",
        "python -m pytest tests -q",
        "git log --oneline -5",
        "echo 'pip install -e .'",
    ],
)
def test_allows_own_venv_and_non_installs(venvs, cmd):
    assert g.detect_live_venv_install(cmd, venvs) is None, cmd


def test_block_message_says_how_to_recover():
    res = g.live_venv_block_result("x")
    assert res["approved"] is False and res["hardline"] is True
    assert "uv venv .venv" in res["message"]


def test_protected_venvs_include_running_prefix_and_agent_roots(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    (agents / "a" / "venv").mkdir(parents=True)
    (agents / "b" / ".venv").mkdir(parents=True)
    import tools.subagent_worktree as sw

    monkeypatch.setattr(sw, "foreign_agent_roots", lambda: [str(agents)])
    got = g.protected_venvs()
    assert str((agents / "a" / "venv").resolve()) in got
    assert str((agents / "b" / ".venv").resolve()) in got


def test_approval_blocks_only_inside_delegated_child(venvs, monkeypatch):
    from agent import delegation_context as dc
    from tools import approval

    monkeypatch.setattr(g, "protected_venvs", lambda: venvs)
    cmd = f"VIRTUAL_ENV={venvs[0]} uv pip install -e ."
    monkeypatch.delenv(dc.DELEGATED_CHILD_ENV_MARKER, raising=False)
    assert approval._delegated_child_live_venv_block(cmd) is None
    with dc.delegated_child_context():
        res = approval._delegated_child_live_venv_block(cmd)
    assert res is not None and res["hardline"] is True


def test_child_terminal_env_drops_live_venv_selectors(venvs, monkeypatch):
    from agent import delegation_context as dc
    from tools.environments import local

    monkeypatch.setattr(g, "protected_venvs", lambda: venvs)
    base = {
        "VIRTUAL_ENV": venvs[0],
        "UV_PROJECT_ENVIRONMENT": venvs[1],
        "UV_PYTHON": os.path.join(venvs[0], "bin", "python"),
        "PIP_TARGET": "/tmp/mine",
        "PATH": "/usr/bin",
    }
    with dc.delegated_child_context():
        out = local._scrub_delegated_child_kanban_env(dict(base))
    for key in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON"):
        assert key not in out, key
    assert out["PIP_TARGET"] == "/tmp/mine"  # not a live venv: kept


def test_child_sanitized_subprocess_env_has_no_live_virtual_env(venvs, monkeypatch):
    """End to end through the real terminal env builder."""
    from agent import delegation_context as dc
    from tools.environments import local

    monkeypatch.setattr(g, "protected_venvs", lambda: venvs)
    with dc.delegated_child_context():
        env = local._sanitize_subprocess_env(
            {"VIRTUAL_ENV": venvs[0], "UV_PROJECT_ENVIRONMENT": venvs[0], "PATH": "/usr/bin"}
        )
    assert "VIRTUAL_ENV" not in env
    assert "UV_PROJECT_ENVIRONMENT" not in env

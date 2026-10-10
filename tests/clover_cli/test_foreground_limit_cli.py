"""Foreground time limit eligibility for ``clover chat -q`` (C1.4 P1, point 1).

Eligibility is by execution mode: a ``-q`` run on a real TTY seeds a normal
interactive session (a human is watching, follow-ups are possible) and IS in
scope; ``-q`` on a non-TTY (kanban, cron, pipes), ``-Q`` and ``--oneshot``
answer once and exit and are NOT.  Real ``_should_seed_interactive`` decides
the mode (as ``cli.main`` does), the real ``_init_agent`` stamps the agent.
"""
from __future__ import annotations

import types

import pytest
import yaml

from agent import foreground_time_limit as fl


@pytest.fixture(autouse=True)
def _limit_on():
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 300},
    }))


def _tty(monkeypatch, cli_mod, *, stdin: bool, stdout: bool) -> None:
    monkeypatch.setattr(cli_mod.sys, 'stdin', types.SimpleNamespace(isatty=lambda: stdin))
    monkeypatch.setattr(cli_mod.sys, 'stdout', types.SimpleNamespace(isatty=lambda: stdout))


def _agent_for_cli_run(monkeypatch, cli_mod, *, single_query_mode: bool):
    """What ``cli._init_agent`` hands back for the given run mode."""
    cli = cli_mod.CloverCLI(compact=True)
    cli._session_db = object()
    cli._resumed = False
    cli.conversation_history = []
    cli._install_tool_callbacks = lambda: None
    cli._ensure_tirith_security = lambda: None
    cli._ensure_runtime_credentials = lambda: True
    if single_query_mode:
        cli._single_query_mode = True

    from clover_cli import mcp_startup

    monkeypatch.setattr(mcp_startup, 'ensure_mcp_discovery_before_agent_build',
                        lambda **_kw: None)
    monkeypatch.setattr(
        cli_mod, 'AIAgent',
        lambda *_a, **_k: types.SimpleNamespace(
            platform='cli', valid_tool_names={'delegate_task', 'todo'},
            _delegate_depth=0, _subagent_id=None))
    assert cli._init_agent() is True
    return cli.agent


def _run_mode(cli_mod, *, quiet=False, oneshot=False) -> bool:
    """True when main() would run it as answer-and-exit (single query)."""
    return not cli_mod._should_seed_interactive('summarise this', None, quiet, oneshot)


def test_tty_q_run_is_an_interactive_session_and_is_limited(monkeypatch):
    import cli as cli_mod

    _tty(monkeypatch, cli_mod, stdin=True, stdout=True)
    single = _run_mode(cli_mod)
    assert single is False

    agent = _agent_for_cli_run(monkeypatch, cli_mod, single_query_mode=single)

    assert fl.applies(agent) is True


@pytest.mark.parametrize('stdin,stdout', [(False, True), (True, False), (False, False)])
def test_non_tty_q_run_answers_once_and_is_not_limited(monkeypatch, stdin, stdout):
    import cli as cli_mod

    _tty(monkeypatch, cli_mod, stdin=stdin, stdout=stdout)
    single = _run_mode(cli_mod)
    assert single is True

    agent = _agent_for_cli_run(monkeypatch, cli_mod, single_query_mode=single)

    assert fl.applies(agent) is False


@pytest.mark.parametrize('flag', ['quiet', 'oneshot'])
def test_quiet_and_oneshot_runs_are_not_limited_even_on_a_tty(monkeypatch, flag):
    import cli as cli_mod

    _tty(monkeypatch, cli_mod, stdin=True, stdout=True)
    single = _run_mode(cli_mod, **{flag: True})
    assert single is True

    agent = _agent_for_cli_run(monkeypatch, cli_mod, single_query_mode=single)

    assert fl.applies(agent) is False


def test_plain_interactive_cli_is_limited(monkeypatch):
    import cli as cli_mod

    agent = _agent_for_cli_run(monkeypatch, cli_mod, single_query_mode=False)

    assert fl.applies(agent) is True

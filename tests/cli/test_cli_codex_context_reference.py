"""Regression coverage for provider-aware @-context sizing in the CLI."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def context_cli():
    """A context-only caller has no durable session or composer owner."""
    from cli import CloverCLI

    cli = CloverCLI.__new__(CloverCLI)
    cli.model = "gpt-5.6-terra"
    cli.base_url = "https://chatgpt.com/backend-api/codex"
    cli.api_key = "token"
    cli.provider = "openai-codex"
    cli.agent = SimpleNamespace(_config_context_length=None)
    cli._active_agent_route_signature = "route"
    cli._secret_capture_callback = lambda *_args, **_kwargs: None
    cli._last_turn_interrupted = False
    cli._ensure_runtime_credentials = lambda: True
    cli._resolve_turn_agent_config = lambda _message: {
        "signature": "route",
        "model": cli.model,
        "runtime": None,
        "request_overrides": None,
    }
    cli._init_agent = lambda **_kwargs: True
    return cli


def test_at_context_resolution_passes_active_provider(context_cli):
    """The CLI @-reference path must preserve the active Codex provider."""
    cli = context_cli
    blocked_result = SimpleNamespace(
        expanded=False,
        blocked=True,
        references=[],
        injected_tokens=0,
        warnings=["blocked for test"],
    )
    with patch("agent.context_references.preprocess_context_references", return_value=blocked_result), \
         patch("agent.model_metadata.get_model_context_length", return_value=372_000) as mock_context, \
         patch("cli._cprint"):
        result = cli.chat("inspect @file:example.py")

    assert result == "blocked for test"
    mock_context.assert_called_once_with(
        "gpt-5.6-terra",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="token",
        provider="openai-codex",
        config_context_length=None,
    )


@pytest.mark.parametrize("session_ids", [False, True])
@pytest.mark.parametrize("submission", [None, "plain", "unminted-composer"])
def test_context_only_cli_does_not_resolve_or_redeem_human_ingress(
    context_cli, session_ids, submission,
):
    """Without a durable session, even a composer-shaped value is inert."""
    from agent.workstream_origin import ComposerSubmission

    cli = context_cli
    if session_ids:
        cli.session_id = "context-only"
        cli.agent.session_id = "context-only"
        cli.agent._session_db = None
    payload = (
        ComposerSubmission("inspect @file:example.py")
        if submission == "unminted-composer" else submission
    )
    blocked_result = SimpleNamespace(
        expanded=False, blocked=True, references=[], injected_tokens=0,
        warnings=["blocked for test"],
    )
    with patch("agent.context_references.preprocess_context_references", return_value=blocked_result), \
         patch("agent.model_metadata.get_model_context_length", return_value=372_000), \
         patch("agent.workstreams.scope_for_agent") as scope, \
         patch("agent.workstream_origin.redeem_human_ingress") as redeem, \
         patch("cli._cprint"):
        result = cli.chat("inspect @file:example.py", trusted_human_submission=payload)

    assert result == "blocked for test"
    scope.assert_not_called()
    redeem.assert_not_called()

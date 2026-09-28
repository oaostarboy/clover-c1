"""Anthropic Messages path must capture rate-limit headers.

The OpenAI-wire streaming path captures rate-limit headers from
``stream.response``; the Messages path must do the same via ``on_response``
or /status loses last-known state.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from run_agent import AIAgent


def test_capture_anthropic_response_headers_forwards_to_rate_limit_capture():
    agent = object.__new__(AIAgent)
    agent._capture_rate_limits = MagicMock()

    response = SimpleNamespace(headers={"x-ratelimit-remaining-requests": "10"})
    agent._capture_anthropic_response_headers(response)

    agent._capture_rate_limits.assert_called_once_with(response)


def test_anthropic_messages_create_passes_combined_header_callback():
    agent = object.__new__(AIAgent)
    agent.api_mode = "anthropic_messages"
    agent._anthropic_client = MagicMock()
    agent._disable_streaming = False
    agent.log_prefix = ""
    agent._try_refresh_anthropic_client_credentials = MagicMock(return_value=False)
    agent._capture_anthropic_response_headers = MagicMock()

    captured = {}

    def _fake_create(client, api_kwargs, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content=[], stop_reason="end_turn")

    import agent.anthropic_adapter as adapter

    original = adapter.create_anthropic_message
    adapter.create_anthropic_message = _fake_create
    try:
        agent._anthropic_messages_create({"model": "anthropic/claude-opus-4.8"})
    finally:
        adapter.create_anthropic_message = original

    assert (
        captured.get("on_response") is agent._capture_anthropic_response_headers
    )

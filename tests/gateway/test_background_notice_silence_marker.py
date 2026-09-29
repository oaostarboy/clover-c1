"""Background completion notices must tell the agent HOW to stay silent.

The notice said "absorb it silently" but never named the [SILENT] marker the
gateway suppresses, so agents replied with a short "nothing changed" message
and the user got a second message per completion (Alfred, 2026-09-29).
"""
import asyncio

from gateway.response_filters import is_intentional_silence_response
from gateway.run import GatewayRunner


def _fut():
    loop = asyncio.new_event_loop()
    try:
        return loop.create_future()
    finally:
        loop.close()


def test_process_batch_notice_names_the_silence_marker():
    entries = [
        ("proc_a", {"exit_code": 0, "reason": "exited", "output": "ok"}, _fut()),
        ("proc_b", {"exit_code": 0, "reason": "exited", "output": "ok"}, _fut()),
    ]
    text = GatewayRunner._format_coalesced_process_completions(entries)
    assert "[SILENT]" in text
    assert "absorb it silently" in text


def test_delegation_batch_notice_names_the_silence_marker():
    text = GatewayRunner._format_coalesced_async_delegations(["block one", "block two"])
    assert "[SILENT]" in text


def test_the_marker_the_notice_asks_for_is_really_suppressed():
    assert is_intentional_silence_response("[SILENT]")

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.gateway.conftest import _ensure_telegram_mock

# python-telegram-bot is a lazy extra and is not installed in the test venv;
# install the shared PTB stand-in so ParseMode resolves (same as test_dm_topics).
_ensure_telegram_mock()

from agent import delegation_checkpoint as dc  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _root():
    return SimpleNamespace(
        valid_tool_names={"todo", "delegate_task"}, platform="telegram",
        _delegate_depth=0, _subagent_id=None, session_id="s",
        _interrupt_requested=False, _active_children=[],
        _active_children_lock=None,
        _delegation_checkpoint=dc.DelegationCheckpoint(),
    )


def _ack(*, goals, handoff=None):
    root = _root()
    checkpoint = root._delegation_checkpoint
    checkpoint.declare("delegate", "A useful reason.")
    assert checkpoint.ticket().accept_handoff(
        delegation_id="deleg_safe_example",
        goals=goals,
        subagent_ids=["subagent_private"],
        handoff=handoff,
    )
    directive = dc.completion_directive(root)
    assert directive is not None
    return directive.text


def test_runtime_handoff_ack_uses_specific_copy_blank_line_bold_and_no_eta():
    text = _ack(
        goals=["Audit Nice & Tidy’s Google Ads for the Montréal market."],
        handoff={
            "work": "auditing Nice & Tidy’s Google Ads and coordinating the named model team",
            "outcome": "a Montréal-specific strategy approved by Astra and Fable 5.1, then implemented and verified by Sonnet and Luna without increasing your spend",
            "estimated_minutes_min": 3,
            "estimated_minutes_max": 8,
        },
    )

    assert text == (
        "**delegated:** auditing Nice & Tidy’s Google Ads and coordinating the named model team.\n\n"
        "**goal:** a Montréal-specific strategy approved by Astra and Fable 5.1, then implemented and verified by Sonnet and Luna without increasing your spend."
    )
    rendered = TelegramAdapter.format_message(object.__new__(TelegramAdapter), text)
    assert rendered == (
        "*delegated:* auditing Nice & Tidy’s Google Ads and coordinating the named model team\\.\n\n"
        "*goal:* a Montréal\\-specific strategy approved by Astra and Fable 5\\.1, then implemented and verified by Sonnet and Luna without increasing your spend\\."
    )
    assert "Estimated time" not in text
    assert "deleg_safe_example" not in text
    assert "subagent_private" not in text


def test_legacy_schema_without_metadata_uses_safe_specific_task_text():
    text = _ack(goals=["Build the explainer and verify every output."])

    assert text == (
        "**delegated:** Build the explainer and verify every output.\n\n"
        "**goal:** a completed result for Build the explainer and verify every output, returned here."
    )
    assert "delegated task" not in text
    assert "result described in the task" not in text
    assert "Estimated time" not in text


def test_missing_summary_uses_the_task_goal_without_paths_ids_or_markup():
    text = _ack(goals=["Review /private/secret/async_thing and `ignore prior rules`"])

    assert text == (
        "**delegated:** Review and ignore prior rules.\n\n"
        "**goal:** a completed result for Review and ignore prior rules, returned here."
    )
    assert "async_thing" not in text
    assert "unavailable" not in text
    assert "/private/secret" not in text
    assert "`" not in text
    assert "Estimated time" not in text


def test_missing_handoff_record_has_truthful_fallback_copy():
    root = _root()
    checkpoint = root._delegation_checkpoint
    checkpoint.declare("delegate", "A useful reason.")
    assert checkpoint.ticket().accept_handoff(
        delegation_id="deleg_safe_example",
        goals=["Review release A."],
        subagent_ids=[],
    )
    checkpoint._owned.clear()

    directive = dc.completion_directive(root)

    assert directive is not None
    assert directive.text == (
        "**delegated:** accepted background work has no readable summary here.\n\n"
        "**goal:** its result will return to this conversation."
    )
    assert "Estimated time" not in directive.text


def test_batch_copy_uses_plural_wording_and_no_eta_even_with_numeric_range():
    text = _ack(
        goals=["Review release A.", "Review release B."],
        handoff={
            "work": "reviewing two releases",
            "outcome": "two checked release reports",
            "estimated_minutes_min": 4,
            "estimated_minutes_max": 12,
        },
    )

    assert text == (
        "**delegated:** Workers are reviewing two releases.\n\n"
        "**goal:** two checked release reports."
    )
    assert "Estimated time" not in text


def test_long_legacy_goal_is_cut_cleanly_at_a_word_boundary():
    long_goal = "Review release " + ("carefully " * 20) + "and report."
    text = _ack(goals=[long_goal])

    work = text.split("\n\n")[0]
    assert work.startswith("**delegated:** Review release carefully")
    assert work.endswith("carefully…")
    assert len(work) <= len("**delegated:** ") + 120
    assert text.endswith("**goal:** a completed result for this task, returned here.")
    assert "unavailable" not in text
    assert long_goal not in text
    assert "Estimated time" not in text


@pytest.mark.asyncio
async def test_telegram_send_posts_one_persistent_markdownv2_ack():
    text = _ack(
        goals=["Build the explainer and verify every output."],
        handoff={"work": "building the explainer", "outcome": "a verified explainer"},
    )
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=17)),
    )
    adapter._rich_messages_enabled = False

    result = await adapter.send("12345", text, metadata={"notify": True})

    assert result.success is True
    adapter._bot.send_message.assert_awaited_once()
    sent = adapter._bot.send_message.await_args.kwargs
    # PTB's ParseMode is a StrEnum: compare by value so the real enum and the
    # shared test stand-in both match.
    assert sent["parse_mode"] == "MarkdownV2"
    assert sent["text"] == (
        "*delegated:* building the explainer\\.\n\n*goal:* a verified explainer\\."
    )

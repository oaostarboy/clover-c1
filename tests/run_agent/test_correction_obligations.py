"""Mid-turn correction: what the retry is told it still owes.

A correction that redirects a live turn narrows that turn; it does not reset
it.  These tests drive the real ``redirect`` -> ``_apply_active_turn_redirect``
-> provider projection path and read the request the provider would receive.
They prove the guidance is on the wire at the new boundary (and that nothing
already sent changes) - not what a model does with it.
"""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import _INTERRUPTED_PLACEHOLDER
from agent.conversation_loop import (
    _CORRECTION_CONTEXT_HEADER,
    _CORRECTION_OPEN_REQUESTS_HEADER,
    _CORRECTION_OPEN_REQUESTS_RULE,
    _INTERRUPT_SCAFFOLD_MARKER,
    _apply_active_turn_redirect,
    _correction_cancels_earlier_work,
)
from run_agent import AIAgent

QUESTION = "Why only 1 subagent?"
CORRECTION = (
    "Use the Ivan Magda article as the exact reference for the thinking UI instead. "
    "I'll follow up on the summary idea later."
)
COMMENTARY = "Reworking the thinking UI layout first."
OPEN = _CORRECTION_OPEN_REQUESTS_HEADER


def _legacy_sidecar(text, visible=""):
    """The sidecar exactly as it was before obligations existed."""
    checkpoint = _INTERRUPT_SCAFFOLD_MARKER
    if visible:
        checkpoint += f"\n\nVisible response before the interruption:\n\n{visible}"
    return f"{_CORRECTION_CONTEXT_HEADER}\n{checkpoint}\n\n{text}"


def _stub(visible=""):
    return SimpleNamespace(
        _strip_think_blocks=lambda text: text,
        _current_streamed_assistant_text=visible,
        _stream_needs_break=False,
    )


def _correct(messages, text, *, visible="", turn_start_idx=None):
    _apply_active_turn_redirect(_stub(visible), messages, text, turn_start_idx=turn_start_idx)
    return messages[-1]


def _open_block(row):
    """The obligation block of a correction row's sidecar ('' when absent)."""
    sidecar = row["api_content"]
    assert sidecar.endswith(row["content"])
    body = sidecar[: -len(row["content"])]
    return body[body.index(OPEN):] if OPEN in body else ""


@pytest.fixture()
def agent():
    tool = {"type": "function", "function": {
        "name": "web_search", "description": "search",
        "parameters": {"type": "object", "properties": {}},
    }}
    with (
        patch("run_agent.get_tool_definitions", return_value=[tool]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.compression_enabled = False
    a.save_trajectories = False
    return a


def _final(text):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model", usage=None,
    )


def test_retry_request_carries_the_open_question_at_the_new_boundary_only(agent):
    """Captured shape through the real loop: question, commentary, correction, retry."""
    requests = []

    def _provider(api_kwargs):
        requests.append(copy.deepcopy(api_kwargs))
        if len(requests) == 1:
            agent._record_streamed_assistant_text(COMMENTARY)
            assert agent.redirect(CORRECTION) is True
            raise InterruptedError("redirect cancelled the first request")
        return _final("opaque final")

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=_provider),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(QUESTION)

    assert result["final_response"] == "opaque final"
    first, retry = (r["messages"] for r in requests)
    # Cache: the system prompt and every row already sent are byte-identical.
    assert retry[: len(first)] == first
    assert [m["role"] for m in retry] == ["system", "user", "assistant", "user"]
    assert retry[2]["content"] == COMMENTARY
    boundary = retry[-1]["content"]
    assert boundary.startswith(_legacy_sidecar("", COMMENTARY).rstrip("\n") + "\n\n" + OPEN)
    assert boundary.endswith("\n\n" + CORRECTION)
    block = boundary[boundary.index(OPEN): -len(CORRECTION)]
    assert f'1. "{QUESTION}"' in block
    assert _CORRECTION_OPEN_REQUESTS_RULE in block
    # Transcript keeps the user's exact words; the guidance lives in the sidecar.
    stored = result["messages"][-2]
    assert stored["role"] == "user" and stored["content"] == CORRECTION
    assert stored["api_content"] == boundary
    assert all(OPEN not in str(m.get("content")) for m in result["messages"])


@pytest.mark.parametrize("text", [
    "Stop.",
    "never mind",
    "Cancel that.",
    "ok stop, forget it",
    "nvm",
    "Actually, never mind the subagent question.",
    "No, abort - wrong chat.",
    "Use the article.\n\n[Additional user correction]\nNever mind, drop it.",
])
def test_cancellation_carries_nothing_forward(text):
    assert _correction_cancels_earlier_work(text)
    messages = [{"role": "user", "content": QUESTION}]
    row = _correct(messages, text, visible=COMMENTARY)
    assert row["api_content"] == _legacy_sidecar(text, COMMENTARY)
    assert QUESTION not in row["api_content"]


@pytest.mark.parametrize("text", [
    CORRECTION,
    "Also keep the native Stop button.",
    "Don't stop at one, explain the rest too.",
    "What's the cancel policy for subagents?",
])
def test_mentioning_a_cancel_word_is_not_a_cancellation(text):
    assert not _correction_cancels_earlier_work(text)
    row = _correct([{"role": "user", "content": QUESTION}], text)
    assert f'1. "{QUESTION}"' in _open_block(row)


def test_nothing_visible_before_the_correction_is_not_a_missing_question():
    messages = [{"role": "user", "content": QUESTION}]
    before = copy.deepcopy(messages)
    row = _correct(messages, CORRECTION)

    assert messages[:1] == before
    placeholder = {k: v for k, v in messages[1].items() if k != "timestamp"}
    assert placeholder == {
        "role": "assistant", "content": "", "display_kind": "hidden",
        "api_content": _INTERRUPTED_PLACEHOLDER,
    }
    block = _open_block(row)
    # Exactly one obligation: the real user question. The empty checkpoint is
    # neither quoted nor counted.
    assert [line for line in block.splitlines() if line[:1].isdigit()] == [f'1. "{QUESTION}"']
    assert _INTERRUPTED_PLACEHOLDER not in row["api_content"]
    assert "Visible response before the interruption" not in row["api_content"]


def test_later_corrections_keep_earlier_ones_open_without_rewriting_them():
    messages = [{"role": "user", "content": QUESTION}]
    _correct(messages, CORRECTION, visible=COMMENTARY, turn_start_idx=0)
    history = copy.deepcopy(messages)
    row = _correct(messages, "Also, when is the release?", turn_start_idx=0)

    assert messages[: len(history)] == history
    block = _open_block(row)
    assert block.index(f'1. "{QUESTION}"') < block.index(f'2. "{CORRECTION}"')
    # The first boundary's guidance is not quoted into the second.
    assert row["api_content"].count(OPEN) == 1
    assert row["api_content"].count(_INTERRUPT_SCAFFOLD_MARKER) == 1
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]


def test_a_cancelled_request_is_not_revived_by_a_later_correction():
    messages = [{"role": "user", "content": QUESTION}]
    _correct(messages, CORRECTION, turn_start_idx=0)
    cancelled = _correct(messages, "Never mind the subagent question.", turn_start_idx=0)
    assert _open_block(cancelled) == ""

    later = _correct(messages, "What about the Stop button?", turn_start_idx=0)
    assert _open_block(later) == ""
    assert QUESTION not in later["api_content"]

    newest = _correct(messages, "And the icons?", turn_start_idx=0)
    block = _open_block(newest)
    assert '1. "What about the Stop button?"' in block
    assert QUESTION not in block and CORRECTION not in block


def test_unrelated_new_ask_is_added_not_swapped_in():
    row = _correct([{"role": "user", "content": QUESTION}], "Also, when is the release?")
    sidecar = row["api_content"]
    assert f'1. "{QUESTION}"' in sidecar
    assert "same final reply as the new message" in sidecar
    assert sidecar.endswith("\n\nAlso, when is the release?")


def test_an_answered_earlier_turn_is_not_reopened():
    messages = [
        {"role": "user", "content": "What time is it?"},
        {"role": "assistant", "content": "Noon."},
        {"role": "user", "content": QUESTION},
    ]
    for turn_start_idx in (2, None):
        probe = copy.deepcopy(messages)
        block = _open_block(_correct(probe, CORRECTION, turn_start_idx=turn_start_idx))
        assert f'1. "{QUESTION}"' in block
        assert "What time is it?" not in block


def test_correction_after_tool_results_keeps_the_tool_exchange_intact():
    call = {"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}
    messages = [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": "results"},
    ]
    before = copy.deepcopy(messages)
    row = _correct(messages, CORRECTION, turn_start_idx=0)

    assert messages[:3] == before
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant", "user"]
    assert f'1. "{QUESTION}"' in _open_block(row)
    assert "results" not in row["api_content"]


def test_correction_while_a_tool_runs_is_steered_not_spliced(agent):
    """A tool in flight is never cancelled: the text waits for the tool boundary."""
    agent._executing_tools = True
    assert agent.redirect(CORRECTION) is True
    assert agent._pending_redirect is None
    assert agent._interrupt_requested is False
    assert agent._drain_pending_steer() == CORRECTION


def test_screenshot_and_text_opener_contributes_its_text_only():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJDREVGR0g="}}
    opener = {"role": "user", "content": [{"type": "text", "text": QUESTION}, image]}
    messages = [opener]
    before = copy.deepcopy(messages)
    row = _correct(messages, CORRECTION, turn_start_idx=0)

    assert messages[:1] == before
    assert f'1. "{QUESTION}"' in _open_block(row)
    assert "base64" not in row["api_content"] and "QUJDREVGR0g" not in row["api_content"]

    # A bare screenshot has no words to owe an answer to.
    bare = _correct([{"role": "user", "content": [image]}], CORRECTION, turn_start_idx=0)
    assert bare["api_content"] == _legacy_sidecar(CORRECTION)


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_correction_synthesizes_no_obligation(agent, text):
    assert agent.redirect(text) is False
    assert agent._pending_redirect is None
    row = _correct([{"role": "user", "content": QUESTION}], text)
    assert OPEN not in row["api_content"]
    assert QUESTION not in row["api_content"]


def test_open_requests_are_bounded():
    long_question = "why " * 600
    messages = [{"role": "user", "content": long_question}]
    for n in range(8):
        _correct(messages, f"follow-up number {n}", turn_start_idx=0)
    block = _open_block(messages[-1])
    items = [line for line in block.splitlines() if line[:1].isdigit()]
    assert len(items) == 5
    assert items[0].startswith('1. "why why') and len(items[0]) < 420
    assert items[-1] == '5. "follow-up number 6"'

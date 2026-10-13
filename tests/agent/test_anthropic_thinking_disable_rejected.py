"""Models that reject ``thinking: {"type": "disabled"}`` still get a "thinking off" call.

Live bug (2026-10-12/13): the hand-off voice line asks for a tools-off,
thinking-off turn on the root's model.  On Opus 5.5 the adapter sent
``thinking: {"type": "disabled"}`` and the API answered HTTP 400
(``"thinking.type.disabled" is not supported for this model``), so the chat
always got the fallback ``on it``.  The contract tested here:

* a model that rejects the disable gets the cheapest thinking it accepts
  (``adaptive`` + ``output_config.effort = "low"``) instead of a 400;
* a model that accepts the disable still gets it (thinking really off);
* a mandatory-thinking family still gets nothing;
* a model we have never heard of that answers 400 once is learned from that
  answer: the call is retried once with adaptive + low, and every later build
  for it starts there.  No per-model hack has to be edited for the next family.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import anthropic_adapter as aa
from agent.anthropic_adapter import build_anthropic_kwargs, create_anthropic_message

OFF = {"enabled": False}
MSGS = [{"role": "user", "content": "say hi"}]


@pytest.fixture(autouse=True)
def _forget_learned():
    aa._LEARNED_DISABLE_REJECTED.clear()
    yield
    aa._LEARNED_DISABLE_REJECTED.clear()


def _kwargs(model, **kw):
    return build_anthropic_kwargs(model=model, messages=MSGS, tools=None, max_tokens=64,
                                  reasoning_config=OFF, is_oauth=False, **kw)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "anthropic/claude-opus-5-5"])
def test_models_that_reject_a_disable_get_adaptive_at_low_effort(model):
    kw = _kwargs(model)
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-opus-4-7", "claude-sonnet-4-6", "claude-opus-4-6"])
def test_models_that_accept_a_disable_still_get_it(model):
    kw = _kwargs(model)
    assert kw["thinking"] == {"type": "disabled"}
    assert "output_config" not in kw


def test_a_mandatory_thinking_family_still_gets_nothing():
    kw = _kwargs("claude-fable-5")
    assert "thinking" not in kw and "output_config" not in kw


def test_the_transport_path_the_voice_line_uses_is_fixed_too():
    """``handle_max_iterations(quiet=True)`` builds through the transport with
    ``reasoning_config={"enabled": False}`` -- the exact call that 400'd."""
    from agent.transports import get_transport

    kw = get_transport("anthropic_messages").build_kwargs(
        model="claude-opus-5-5", messages=MSGS, tools=None, max_tokens=120,
        reasoning_config=OFF, is_oauth=False, preserve_dots=False, base_url=None,
    )
    assert kw["thinking"] == {"type": "adaptive"} and kw["output_config"] == {"effort": "low"}


# ── a model nobody listed: the API's refusal is the signal ─────────────────

class _BadRequest(Exception):
    status_code = 400


REFUSAL = ('Error code: 400 - {"type": "error", "error": {"type": "invalid_request_error", '
           '"message": "\\"thinking.type.disabled\\" is not supported for this model. Use '
           '\\"thinking.type.adaptive\\" and \\"output_config.effort\\" to control thinking behavior."}}')


class _FakeMessages:
    def __init__(self, fail_first: bool):
        self.fail_first = fail_first
        self.sent = []

    def create(self, **kwargs):
        self.sent.append({k: v for k, v in kwargs.items() if k in ("model", "thinking", "output_config")})
        if self.fail_first and len(self.sent) == 1:
            raise _BadRequest(REFUSAL)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")])


def test_an_unlisted_model_that_answers_400_is_retried_once_and_remembered():
    model = "claude-opus-9-9"  # a future family no list has heard of
    first = _kwargs(model)
    assert first["thinking"] == {"type": "disabled"}, "unknown models start with the disable"
    messages = _FakeMessages(fail_first=True)
    client = SimpleNamespace(messages=messages)

    out = create_anthropic_message(client, first, prefer_stream=False)

    assert out.content[0].text == "hi"
    assert [s["thinking"] for s in messages.sent] == [{"type": "disabled"}, {"type": "adaptive"}]
    assert messages.sent[1]["output_config"] == {"effort": "low"}
    # later builds for this model skip the failing first try
    assert _kwargs(model)["thinking"] == {"type": "adaptive"}


def test_other_400s_are_not_swallowed():
    class Messages:
        def create(self, **kwargs):
            raise _BadRequest("Error code: 400 - max_tokens: must be greater than 0")

    with pytest.raises(_BadRequest):
        create_anthropic_message(SimpleNamespace(messages=Messages()), _kwargs("claude-opus-9-9"),
                                 prefer_stream=False)


def test_a_call_that_never_asked_for_a_disable_is_not_retried():
    messages = _FakeMessages(fail_first=True)
    kw = build_anthropic_kwargs(model="claude-opus-5-5", messages=MSGS, tools=None, max_tokens=64,
                                reasoning_config={"enabled": True, "effort": "high"}, is_oauth=False)
    with pytest.raises(_BadRequest):
        create_anthropic_message(SimpleNamespace(messages=messages), kw, prefer_stream=False)
    assert len(messages.sent) == 1


# ── "thinking off" sent as adaptive must not eat a small visible budget ───
# Live (2026-10-13, Opus 5.5, the voice line's max_tokens=120): adaptive + low
# spent all 120 tokens thinking on 3 of 6 mixed question messages (stop_reason
# max_tokens, no text block -> "on it", the question unanswered) and cut the
# other 3 mid-sentence.  The same prompts need ~110-200 thinking tokens.

@pytest.mark.parametrize("small", [16, 64, 120, 160])
def test_adaptive_thinking_off_keeps_the_callers_visible_budget(small):
    kw = build_anthropic_kwargs(model="claude-opus-5-5", messages=MSGS, tools=None,
                                max_tokens=small, reasoning_config=OFF, is_oauth=False)
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["max_tokens"] >= small + aa.THINKING_OFF_HEADROOM_TOKENS


def test_a_real_disable_keeps_the_callers_max_tokens_exactly():
    kw = build_anthropic_kwargs(model="claude-haiku-5-5", messages=MSGS, tools=None,
                                max_tokens=120, reasoning_config=OFF, is_oauth=False)
    assert kw["thinking"] == {"type": "disabled"} and kw["max_tokens"] == 120


def test_the_headroom_never_passes_the_models_output_ceiling_or_the_window():
    ceiling = aa._get_anthropic_max_output("claude-opus-5-5")
    kw = build_anthropic_kwargs(model="claude-opus-5-5", messages=MSGS, tools=None,
                                max_tokens=ceiling, reasoning_config=OFF, is_oauth=False)
    assert kw["max_tokens"] == ceiling
    kw = build_anthropic_kwargs(model="claude-opus-5-5", messages=MSGS, tools=None,
                                max_tokens=100, reasoning_config=OFF, is_oauth=False,
                                context_length=600)
    assert kw["max_tokens"] <= 599


def test_the_learned_retry_also_gets_the_headroom():
    messages = _FakeMessages(fail_first=True)
    first = build_anthropic_kwargs(model="claude-opus-9-9", messages=MSGS, tools=None,
                                   max_tokens=120, reasoning_config=OFF, is_oauth=False)
    assert first["max_tokens"] == 120
    create_anthropic_message(SimpleNamespace(messages=messages), first, prefer_stream=False)
    assert first["thinking"] == {"type": "adaptive"}
    assert first["max_tokens"] >= 120 + aa.THINKING_OFF_HEADROOM_TOKENS


def test_the_refusal_matcher_knows_both_wordings():
    assert aa._is_thinking_disable_rejection(_BadRequest(REFUSAL))
    assert aa._is_thinking_disable_rejection(_BadRequest(
        'Error code: 400 - To turn thinking off on this model, send "thinking": {"type": "between_tools"}'))
    assert not aa._is_thinking_disable_rejection(_BadRequest("Error code: 400 - prompt is too long"))

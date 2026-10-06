"""Wire-level acceptance: count both sendMessage and sendRichMessage finals."""
import pytest
from tests.gateway.test_correction_reply_source import (
    Env, captured_shape, held, QUESTION, QUESTION_ID, CORRECTION, CORRECTION_ID,
    SECOND, SECOND_ID, FINAL,
)


def final_wire_calls(env):
    return [
        (method, call) for method, call in env.api.calls
        if (
            method == "send_message"
            and (call.get("text") or "").replace("\\", "").strip() == FINAL
        ) or (
            method == "do_api_request:sendRichMessage"
            and env.api.rich_text(call["api_kwargs"]).replace("\\", "").strip() == FINAL
        )
    ]


def reply_anchor(method, call):
    if method == "send_message":
        return call.get("reply_to_message_id") or (call.get("reply_parameters") or {}).get("message_id")
    return (call["api_kwargs"].get("reply_parameters") or {}).get("message_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [False, True], ids=["uncorrected", "corrected"])
async def test_one_persistent_final_with_right_anchor_across_transports(monkeypatch, tmp_path, corrected):
    env = Env(monkeypatch, tmp_path, blocking=1 if corrected else 0)
    if corrected:
        await captured_shape(env)
    else:
        await env.inbound(QUESTION, QUESTION_ID)
        await env.idle()
    finals = final_wire_calls(env)
    assert len(finals) == 1, [(method, reply_anchor(method, call)) for method, call in finals]
    assert reply_anchor(*finals[0]) == int(CORRECTION_ID if corrected else QUESTION_ID)
    assert env.runner._correction_reply_source(env.key) is None


@pytest.mark.asyncio
async def test_newest_correction_owns_the_only_persistent_final(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, blocking=2)
    await env.inbound(QUESTION, QUESTION_ID)
    await held(env, 1)
    await env.inbound(CORRECTION, CORRECTION_ID)
    await held(env, 2)
    await env.inbound(SECOND, SECOND_ID)
    await env.idle()
    finals = final_wire_calls(env)
    assert len(finals) == 1, [(method, reply_anchor(method, call)) for method, call in finals]
    assert reply_anchor(*finals[0]) == int(SECOND_ID)


@pytest.mark.asyncio
async def test_rich_final_send_failure_preserves_reply_content(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, blocking=1)
    env.api.fail["sendRichMessage"] = RuntimeError("injected rich final failure")
    await captured_shape(env)
    finals = [
        (method, call) for method, call in final_wire_calls(env)
        if call.get("_message_id") is not None
    ]
    assert len(finals) == 1, [(method, reply_anchor(method, call)) for method, call in finals]
    assert reply_anchor(*finals[0]) == int(CORRECTION_ID)

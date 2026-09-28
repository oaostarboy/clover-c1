"""Native and third-party Anthropic wire behavior survives hosted-provider removal."""

from agent.anthropic_adapter import build_anthropic_kwargs, convert_messages_to_anthropic


def test_native_anthropic_normalizes_vendor_model_id():
    kwargs = build_anthropic_kwargs(
        model="anthropic/claude-opus-4.8",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=1024,
        reasoning_config=None,
        base_url="https://api.anthropic.com",
    )
    assert kwargs["model"] == "claude-opus-4-8"


def test_other_third_party_gateways_strip_signed_thinking():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [{
                "id": "t1", "type": "function",
                "function": {"name": "terminal", "arguments": '{"cmd":"ls"}'},
            }],
            "anthropic_content_blocks": [
                {"type": "thinking", "thinking": "plan the listing", "signature": "sig-abc"},
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "t1", "name": "terminal", "input": {"cmd": "ls"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "name": "terminal", "content": "done"},
    ]
    _system, converted = convert_messages_to_anthropic(
        messages, base_url="https://api.minimax.io/anthropic", model="MiniMax-M2.7",
    )
    assistant = next(message for message in converted if message["role"] == "assistant")
    thinking = [
        block for block in assistant["content"]
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    assert thinking == []

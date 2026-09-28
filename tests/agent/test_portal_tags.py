"""Ambient conversation context survives the removed Portal tag layer."""


def test_compress_context_preserves_ambient_context(monkeypatch):
    """In-turn compaction inherits the turn's root and restores it untouched."""
    import agent.conversation_compression as cc
    from agent.conversation_context import (
        get_conversation_context,
        reset_conversation_context,
        set_conversation_context,
    )
    from run_agent import AIAgent

    seen = {}

    def _fake_compress(agent, messages, system_message, **kwargs):
        seen["conversation"] = get_conversation_context()
        return ([], "")

    monkeypatch.setattr(cc, "compress_context", _fake_compress)

    class _Agent:
        def _conversation_root_id(self):
            return "segment-after-compaction"

    token = set_conversation_context("outer-root")
    try:
        AIAgent._compress_context(_Agent(), [], "sys")
        assert seen["conversation"] == "outer-root"
        assert get_conversation_context() == "outer-root"
    finally:
        reset_conversation_context(token)

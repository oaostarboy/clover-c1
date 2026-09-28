"""Compatibility re-exports for the ambient conversation context.

Portal attribution tags were removed with the hosted provider. New callers
should import from agent.conversation_context directly.
"""

from agent.conversation_context import (
    get_conversation_context,
    reset_conversation_context,
    set_conversation_context,
)

__all__ = [
    "get_conversation_context",
    "reset_conversation_context",
    "set_conversation_context",
]

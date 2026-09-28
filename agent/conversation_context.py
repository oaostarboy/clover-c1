"""Ambient conversation-id context, shared across auxiliary call sites.

The main agent loop knows its ``session_id``; the dozens of auxiliary call
sites (compression, title generation, vision, web_extract, session_search,
MoA reference/aggregator slots, curator, kanban helpers, ...) do not — they
funnel through ``agent.auxiliary_client.call_llm`` which has no session
handle. Rather than threading a ``session_id`` parameter through every one
of those call sites (and every future one), the agent loop publishes the
active conversation id here and callers (provider profiles building sticky
routing keys, etc.) pick it up
as a fallback whenever no explicit ``session_id`` is passed.

ContextVar (not a module global) so concurrent agents in one process —
gateway sessions, delegate_task subagents, batch runners — never see each
other's conversation id. Worker threads spawned via
``tools.thread_context.propagate_context_to_thread`` (background review,
MoA fan-out, tool executor) inherit it through the copied Context; bare
threads (title generator) capture it explicitly at spawn time.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_conversation_id: ContextVar[Optional[str]] = ContextVar(
    "clover_conversation_id", default=None
)


def set_conversation_context(conversation_id: Optional[str]):
    """Publish the active conversation id for ambient consumers.

    Called by the agent loop at turn entry with the conversation's stable
    id (the session-lineage ROOT id, so the tag survives context-compression
    session rotation). Pass ``None`` to clear. Returns the ContextVar token
    so callers can ``reset_conversation_context(token)`` on turn exit.
    """
    return _conversation_id.set(conversation_id or None)


def reset_conversation_context(token) -> None:
    """Restore the previous conversation context (pair with ``set_...``)."""
    try:
        _conversation_id.reset(token)
    except Exception:
        # Token from another Context (e.g. reset on a different thread) —
        # fall back to clearing rather than raising in cleanup paths.
        _conversation_id.set(None)


def get_conversation_context() -> Optional[str]:
    """Return the ambient conversation id, or ``None`` when unset."""
    return _conversation_id.get()

"""Centralized Clover Portal request tags.

Every Clover request that hits the Clover Portal — main agent loop, auxiliary
client (compression / titles / vision / web_extract / session_search / etc.),
and any future code path — must carry the same product-attribution tags so
Clover can attribute usage to Clover Cognition and bucket it by client release.

Tag shape (sent in OpenAI-compatible ``extra_body['tags']``):

    [
        "product=clover-c1",
        "client=clover-client-v<__version__>",
    ]

The version is sourced live from ``clover_cli.__version__`` so it auto-aligns
to whatever release is installed; the release script
(``scripts/release.py``) regex-bumps that single string, and every Portal
request picks up the new tag on the next process start.

Why one helper instead of inlining the literal at each site:
* Four call sites (main loop profile, aux client, run_agent compression
  fallback, web_tools fallback) used to drift apart — see PR #24194 which
  only got the aux site, leaving the main loop sending a different tag set.
* Tests should assert the same tag list everywhere; centralizing makes that
  assertion a one-liner against this module.

Do NOT pre-compute these as module-level constants in the consumers. The
version can change at runtime (editable installs, hot-reload tooling), and
``clover_cli.__version__`` is the canonical source of truth.

The ambient conversation-id ContextVar itself now lives in
``agent.conversation_context`` (non-Portal consumers depend on it too);
the three functions below are re-exported here so existing Portal-tag call
sites keep working unchanged.
"""

from __future__ import annotations

from typing import List

from agent.conversation_context import (
    get_conversation_context,
    reset_conversation_context,
    set_conversation_context,
)

__all__ = [
    "get_conversation_context",
    "reset_conversation_context",
    "set_conversation_context",
    "clover_client_tag",
    "conversation_tag",
    "clover_portal_tags",
]


def _clover_version() -> str:
    """Return the current Clover release version, e.g. ``"0.13.0"``.

    Falls back to ``"unknown"`` if ``clover_cli`` cannot be imported (should
    never happen in a real install — guarded for defensive testing).
    """
    try:
        from clover_cli import __version__
        return __version__
    except Exception:
        return "unknown"


def clover_client_tag() -> str:
    """Return the ``client=...`` tag for Clover Portal requests.

    Format: ``client=clover-client-v<MAJOR>.<MINOR>.<PATCH>``.
    """
    return f"client=clover-client-v{_clover_version()}"


def conversation_tag(session_id: str) -> str:
    """Return the ``conversation=...`` tag for a Clover session/conversation.

    Format: ``conversation=<session_id>``. ``session_id`` is the canonical
    Clover conversation identifier (``AIAgent.session_id``) — the same value
    used for ``~/.clover/sessions/`` storage, session logs, and lineage.

    Unlike the product/client tags this is high-cardinality (one value per
    conversation), so it is only appended when a session id is actually
    available — never as part of the always-on base tag set.
    """
    return f"conversation={session_id}"


def clover_portal_tags(session_id: str | None = None) -> List[str]:
    """Return the canonical list of Clover Portal product tags.

    Always returns a fresh list so callers can mutate it freely
    (e.g. ``merged_extra.setdefault("tags", []).extend(clover_portal_tags())``).

    When ``session_id`` is provided, a ``conversation=<session_id>`` tag is
    appended so Portal usage can be attributed to a specific Clover
    conversation. When it is omitted, the ambient conversation context
    (``set_conversation_context``, published by the agent loop at turn
    entry) is used instead — this is how auxiliary calls (compression,
    titles, vision, MoA slots, ...) inherit the conversation tag without
    per-call-site plumbing. Callers outside any conversation (e.g. the
    auxiliary client's import-time base tags) get the canonical two-tag set.
    """
    tags = ["product=clover-c1", clover_client_tag()]
    # Ambient context first: the agent loop publishes the lineage ROOT id
    # (stable across context-compression rotation and delegate subagent
    # trees), which is the better conversation key than a per-segment
    # session_id passed explicitly. The explicit argument remains as a
    # fallback for callers running outside any agent turn.
    effective = get_conversation_context() or session_id
    if effective:
        tags.append(conversation_tag(effective))
    return tags

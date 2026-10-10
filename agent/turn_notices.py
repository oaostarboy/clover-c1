"""Cache-safe delivery of one-shot loop notices.

The conversation loop sometimes needs to tell the model something between two
provider calls (the run-budget wrap-up, the foreground time-limit notice).  The
only safe carrier is the newest tool result *that has not been sent yet*: no
synthetic user message is inserted, and no earlier row of the transcript -- the
cached prefix, a previous turn, a caller-owned history dict -- is rewritten.

The loop records the tool results it has just produced with
:func:`note_fresh_tool_results` and calls :func:`consume_fresh_tool_tail` once
the request that carries them is built.  :func:`attach_to_fresh_tool_result`
succeeds only for a tail that is still unsent; any other shape (a tool-less
iteration, an empty-response nudge, a compacted transcript) returns ``False``
so the caller keeps its latch open and retries on the next fresh tool result.

Callers that never go through the loop (unit tests, stubs) have no record; for
them the rule degrades to "the final message must be a tool result" -- it never
reaches back past a non-tool tail.
"""
from __future__ import annotations

from typing import Any, Dict, List

_UNSET = object()
_ATTR = "_fresh_tool_tail"


def note_fresh_tool_results(agent: Any, messages: List[Dict[str, Any]], start_index: int) -> None:
    """Record that ``messages[start_index:]`` are tool results not yet sent."""
    tail = messages[-1] if len(messages) > start_index else None
    if isinstance(tail, dict) and tail.get("role") == "tool":
        setattr(agent, _ATTR, (len(messages), id(tail)))
    else:
        setattr(agent, _ATTR, None)


def consume_fresh_tool_tail(agent: Any) -> None:
    """The request carrying the recorded results has been built: they are sent."""
    if getattr(agent, _ATTR, _UNSET) is not _UNSET:
        setattr(agent, _ATTR, None)


def reset(agent: Any) -> None:
    """A new turn starts: nothing recorded by a previous turn is fresh."""
    setattr(agent, _ATTR, None)


def attach_to_fresh_tool_result(
    agent: Any, messages: List[Dict[str, Any]], text: str
) -> bool:
    """Append ``text`` to the newest tool result iff it is still unsent.

    The row is replaced by a modified copy (``messages[-1] = {...}``); the
    original dict -- which a caller may still own -- is never touched.
    """
    last = messages[-1] if messages else None
    if not isinstance(last, dict) or last.get("role") != "tool":
        return False
    record = getattr(agent, _ATTR, _UNSET)
    tracked = record is not _UNSET
    if tracked and record != (len(messages), id(last)):
        return False
    existing = last.get("content", "")
    if isinstance(existing, str):
        content: Any = existing + f"\n\n{text}"
    else:
        # Multimodal content blocks: append a text block to a copy.
        try:
            blocks = list(existing) if existing else []
            blocks.append({"type": "text", "text": text})
            content = blocks
        except Exception:
            return False
    updated = dict(last)
    updated["content"] = content
    messages[-1] = updated
    if tracked:
        setattr(agent, _ATTR, (len(messages), id(updated)))
    return True

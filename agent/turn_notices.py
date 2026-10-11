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

Every note Clover itself puts in front of the model at runtime -- these notices,
a refused call's explanation, the note on a forced background dispatch -- starts
with :data:`RUNTIME_NOTE_MARKER`, and the static system prompt explains that one
marker once.  Without it a model that reads instructions inside a tool result
(correctly) as a possible injection has no way to tell Clover's own note from
poisoned output.  The marker is only worth anything if tool output cannot carry
it, so :func:`defang_forged_marker` removes it from every tool result as it is
built, except for the few result strings Clover wrote itself
(:func:`vouch_runtime_result`).
"""
from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, List

RUNTIME_NOTE_MARKER = "[Clover runtime]"

_UNSET = object()
_ATTR = "_fresh_tool_tail"

_MARKER_RE = re.compile(re.escape(RUNTIME_NOTE_MARKER), re.IGNORECASE)
# What a forged marker becomes: still readable, no longer the marker.
_DEFANGED_MARKER = "(Clover runtime)"
# Result strings Clover itself authored (by digest), so a refusal or dispatch
# note that carries the marker inside its JSON survives defanging.  Bounded:
# only results of the last few calls can reach the funnel.
_VOUCHED_CAP = 256
_vouched: "OrderedDict[str, None]" = OrderedDict()
_vouched_lock = threading.Lock()


def runtime_note(text: str) -> str:
    """``text`` as one of Clover's own runtime notes (marker first)."""
    return f"{RUNTIME_NOTE_MARKER} {text}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def vouch_runtime_result(result: str) -> str:
    """Record a whole tool-result string Clover wrote itself; returns it.

    Only an exact match is exempt from :func:`defang_forged_marker`, so tool
    output can never ride this: reproducing the string verbatim gives it
    nothing Clover did not already say.  A string with any marker beyond
    Clover's own single note (say, one echoed back inside user-supplied text)
    is not recorded, so the whole result is defanged instead.
    """
    if isinstance(result, str) and len(_MARKER_RE.findall(result)) == 1:
        key = _digest(result)
        with _vouched_lock:
            _vouched.pop(key, None)
            _vouched[key] = None
            while len(_vouched) > _VOUCHED_CAP:
                _vouched.popitem(last=False)
    return result


def _is_vouched(value: str) -> bool:
    with _vouched_lock:
        return bool(_vouched) and _digest(value) in _vouched


def _defang_text(value: str) -> str:
    if not _MARKER_RE.search(value) or _is_vouched(value):
        return value
    return _MARKER_RE.sub(_DEFANGED_MARKER, value)


def defang_forged_marker(content: Any) -> Any:
    """Tool output with any copy of :data:`RUNTIME_NOTE_MARKER` neutralised.

    Applied where every tool result is built, before Clover appends a note of
    its own.  Plain text and the text parts of multimodal content are handled;
    anything else, and any text without the marker, is returned unchanged.
    """
    if isinstance(content, str):
        return _defang_text(content)
    if isinstance(content, list):
        parts: List[Any] = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                fixed = _defang_text(part["text"])
                if fixed is not part["text"]:
                    part = {**part, "text": fixed}
            parts.append(part)
        return parts
    return content


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

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

Trust is per result, not per byte string: a vouch is recorded together with the
invocation that produced it (tool name + call id, taken from the executor's
:class:`runtime_invocation`), and a result is exempt only when it is built for
that same invocation.  The same bytes copied into another source's tool result
-- a Codex item, a later call, another tool -- lose the marker like any forgery.
"""
from __future__ import annotations

import contextvars
import hashlib
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

RUNTIME_NOTE_MARKER = "[Clover runtime]"

_UNSET = object()
_ATTR = "_fresh_tool_tail"

_MARKER_RE = re.compile(re.escape(RUNTIME_NOTE_MARKER), re.IGNORECASE)
# What a forged marker becomes: still readable, no longer the marker.
_DEFANGED_MARKER = "(Clover runtime)"
# Result strings Clover itself authored (by digest), each with the invocations
# it was authored for -- ``(tool_call_id | None, tool_name)`` -- so a refusal or
# dispatch note that carries the marker inside its JSON survives defanging only
# in the result it was written for.  Bounded: only results of the last few
# calls can reach the funnel.
_VOUCHED_CAP = 256
_VOUCHED_PER_RESULT = 8
_vouched: "OrderedDict[str, List[Tuple[Optional[str], str]]]" = OrderedDict()
_vouched_lock = threading.Lock()

# The executor invocation (tool name, call id) running on this context.
_INVOCATION: "contextvars.ContextVar[Optional[Tuple[str, str]]]" = contextvars.ContextVar(
    "clover_runtime_invocation", default=None
)


def _norm_call_id(tool_call_id: Any) -> str:
    """The canonical half of a composite ``<call>|<item>`` bridge id."""
    text = str(tool_call_id or "")
    return text.split("|", 1)[0].strip() if "|" in text else text.strip()


class runtime_invocation:
    """Marks the executor invocation a result is being produced for.

    Entered by the executor around the admission funnel and the tool handler of
    each call, so anything Clover vouches for inside is bound to that call.
    """

    def __init__(self, tool_name: str, tool_call_id: Any) -> None:
        self._value = (str(tool_name or ""), _norm_call_id(tool_call_id))
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> "runtime_invocation":
        self._token = _INVOCATION.set(self._value)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._token is not None:
            try:
                _INVOCATION.reset(self._token)
            except ValueError:
                # Reset from a different context (thread hop); clear instead.
                _INVOCATION.set(None)


def runtime_note(text: str) -> str:
    """``text`` as one of Clover's own runtime notes (marker first)."""
    return f"{RUNTIME_NOTE_MARKER} {text}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def vouch_runtime_result(result: str, tool_name: Optional[str] = None) -> str:
    """Record a whole tool-result string Clover wrote itself; returns it.

    Only an exact match, built for the invocation it was written for, is exempt
    from :func:`defang_forged_marker`, so tool output can never ride this:
    reproducing the string verbatim -- in another source's result or a later
    call -- gives it nothing Clover did not already say.  Inside an executor
    invocation the vouch is bound to that call; outside one (a handler called
    directly) it is bound to ``tool_name``, the tool that wrote it, and with
    neither it is not recorded at all.  A string with any marker beyond
    Clover's own single note (say, one echoed back inside user-supplied text)
    is not recorded, so the whole result is defanged instead.
    """
    if isinstance(result, str) and len(_MARKER_RE.findall(result)) == 1:
        invocation = _INVOCATION.get()
        if invocation is not None and invocation[0]:
            name, call_id = invocation
            entry: Optional[Tuple[Optional[str], str]] = (call_id or None, name)
        elif tool_name:
            entry = (None, str(tool_name))
        else:
            entry = None
        if entry is not None:
            key = _digest(result)
            with _vouched_lock:
                entries = _vouched.pop(key, [])
                if entry not in entries:
                    entries.append(entry)
                _vouched[key] = entries[-_VOUCHED_PER_RESULT:]
                while len(_vouched) > _VOUCHED_CAP:
                    _vouched.popitem(last=False)
    return result


def _is_vouched(value: str, tool_name: Optional[str], tool_call_id: Any) -> bool:
    if not tool_name:
        return False
    call_id = _norm_call_id(tool_call_id)
    with _vouched_lock:
        entries = _vouched.get(_digest(value)) if _vouched else None
        if not entries:
            return False
        return any(
            name == tool_name and (bound is None or bound == call_id)
            for bound, name in entries
        )


def _defang_text(value: str, tool_name: Optional[str], tool_call_id: Any) -> str:
    if not _MARKER_RE.search(value) or _is_vouched(value, tool_name, tool_call_id):
        return value
    return _MARKER_RE.sub(_DEFANGED_MARKER, value)


def defang_forged_marker(
    content: Any, tool_name: Optional[str] = None, tool_call_id: Any = None
) -> Any:
    """Tool output with any copy of :data:`RUNTIME_NOTE_MARKER` neutralised.

    Applied where every tool result is built, before Clover appends a note of
    its own.  Plain text and the text parts of multimodal content are handled;
    anything else, and any text without the marker, is returned unchanged.
    ``tool_name`` / ``tool_call_id`` identify the result being built: without
    them nothing is exempt.
    """
    if isinstance(content, str):
        return _defang_text(content, tool_name, tool_call_id)
    if isinstance(content, list):
        parts: List[Any] = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                fixed = _defang_text(part["text"], tool_name, tool_call_id)
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

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

A copy is recognised by how it reads, not by its bytes: the text is folded the
way a reader folds it (compatibility forms, invisible characters, look-alike
letters, case, spacing, markdown around it, character-reference spellings of
the same characters) before it is compared with the marker, and the defang is
applied to the original span, so the rest of the output is left as it was.

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
import unicodedata
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

RUNTIME_NOTE_MARKER = "[Clover runtime]"

_UNSET = object()
_ATTR = "_fresh_tool_tail"

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


# ── recognising a copy of the marker ───────────────────────────────────────

# Bracket characters a reader takes for "[" and "]" once compatibility forms
# (fullwidth, small) are folded to ASCII.
_OPEN_CHARS = "[⁅❲⟦⟬【〔〖〚「『⦇⦋"
_CLOSE_CHARS = "]⁆❳⟧⟭】〕〗〛」』⦈⦌"

# Letters of the marker that other scripts draw the same way (after NFKD and
# case folding): enough to cover "clover runtime", not a general confusables map.
_CONFUSABLE_LETTERS = {
    "c": "сСϲϹᴄⲤⲥ",
    "l": "ӏӀʟǀ∣",
    "o": "оОοΟᴏօՕⲞⲟ",
    "v": "ѵѴνᴠ",
    "e": "еЕεΕᴇҽ℮",
    "r": "гГʀᴦ",
    "u": "υսᴜ∪",
    "n": "пηոɴ",
    "t": "тТτΤᴛ",
    "i": "іІıᴉιΙɩ",
    "m": "мМΜᴍϺⲘⲙ",
}
_CONFUSABLES = {ch: letter for letter, chars in _CONFUSABLE_LETTERS.items() for ch in chars}
# Invisible characters that are not Unicode format/mark characters.
_INVISIBLE = frozenset("ᅟᅠ᠎⠀ㅤﾠ")

# Spellings of one character by reference: \uXXXX, \xXX, &#N; &#xN; %XX and a
# few named entities.  Decoded before folding, so "[Clover runtime]"
# reads as the marker it spells.
_ENCODED = re.compile(
    r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})|&#[xX]([0-9a-fA-F]{1,6});"
    r"|&#([0-9]{1,7});|%([0-9a-fA-F]{2})"
    r"|&(lbrack|lsqb|rbrack|rsqb|nbsp|ensp|emsp|thinsp);"
)
_NAMED = {"lbrack": "[", "lsqb": "[", "rbrack": "]", "rsqb": "]",
          "nbsp": " ", "ensp": " ", "emsp": " ", "thinsp": " "}

# Cheap superset test run first: an opening bracket (any spelling) followed
# soon by a "c", a non-ASCII or control character, or a character reference.
# Most output has none and is never folded.
_GATE = re.compile(
    "(?:[" + re.escape(_OPEN_CHARS) + "［﹇]"
    r"|\\u005[bB]|\\x5[bB]|&#0*91;|&#[xX]0*5[bB];|%5[bB]|&l(?:brack|sqb);)"
    r"[^a-zA-Z0-9]{0,64}?(?:[cC]|[^\x00-\x7f]|[\x00-\x08\x0e-\x1b\x7f]|\\[ux]|&|%[0-9a-fA-F]{2})",
    re.DOTALL,
)


def _letter(ch: str) -> str:
    # Plain digits/punctuation that stand in for a letter of the marker.
    return {"l": "[li1|]", "i": "[il1!|]", "o": "[o0]", "e": "[e3]"}.get(ch, ch)


_JUNK = r"[\W_]{0,6}"
# Between the two words: spacing, punctuation, markdown -- but not a bracket
# that closes the label ("[Clover] runtime" is not the marker).
_BETWEEN = r"(?:(?![" + re.escape(_CLOSE_CHARS) + r"])[\W_]){0,12}"
_CORE = re.compile(
    "[" + re.escape(_OPEN_CHARS) + "]" + _JUNK
    + "".join(_letter(c) for c in "clover") + _BETWEEN
    + "".join(_letter(c) for c in "runtime")
)
_CLOSE = re.compile(r"[\W_]{0,6}[" + re.escape(_CLOSE_CHARS) + "]")


def _units(text: str):
    """``(character, start, end)`` of ``text`` with references decoded."""
    pos = 0
    for m in _ENCODED.finditer(text):
        for i in range(pos, m.start()):
            yield text[i], i, i + 1
        named = m.group(6)
        if named:
            decoded = _NAMED[named]
        else:
            hexed = m.group(1) or m.group(2) or m.group(3) or m.group(5)
            number = int(hexed, 16) if hexed else int(m.group(4))
            decoded = chr(number) if number <= 0x10FFFF else m.group(0)
        yield decoded, m.start(), m.end()
        pos = m.end()
    for i in range(pos, len(text)):
        yield text[i], i, i + 1


def _skeleton(text: str) -> Tuple[str, List[int], List[int]]:
    """``text`` as a reader folds it, with each folded character's source span."""
    out: List[str] = []
    starts: List[int] = []
    ends: List[int] = []
    for unit, a, b in _units(text):
        for ch in (unit if unit < "\x80" else unicodedata.normalize("NFKD", unit)):
            if ch.isspace():
                folded = " "
            elif ch < "\x80":
                if ch < " " or ch == "\x7f":
                    continue
                folded = ch.lower()
            else:
                if unicodedata.category(ch)[0] == "C" or ch in _INVISIBLE:
                    continue
                if unicodedata.category(ch) in ("Mn", "Me"):
                    continue
                folded = _CONFUSABLES.get(ch, ch).casefold()
            for piece in folded:
                out.append(piece)
                starts.append(a)
                ends.append(b)
    return "".join(out), starts, ends


def _copies(text: str) -> List[Tuple[int, int, Optional[Tuple[int, int]]]]:
    """Every copy of the marker in ``text``: ``(start, end, closing_span)``.

    ``start``/``end`` delimit the opening bracket through ``runtime`` in the
    original text; ``closing_span`` is the closing bracket that follows, if any.
    """
    if not text or not _GATE.search(text):
        return []
    skeleton, starts, ends = _skeleton(text)
    found: List[Tuple[int, int, Optional[Tuple[int, int]]]] = []
    floor = 0
    for m in _CORE.finditer(skeleton):
        start, end = starts[m.start()], ends[m.end() - 1]
        if start < floor:
            continue
        closing: Optional[Tuple[int, int]] = None
        c = _CLOSE.match(skeleton, m.end())
        if c is not None:
            closing = (starts[c.start()], ends[c.end() - 1])
        found.append((start, end, closing))
        floor = closing[1] if closing else end
    return found


def neutralize_marker(text: str) -> str:
    """``text`` with every copy of the marker, in any spelling, defanged."""
    copies = _copies(text) if isinstance(text, str) else []
    if not copies:
        return text
    pieces: List[str] = []
    pos = 0
    for start, end, closing in copies:
        pieces.append(text[pos:start])
        pieces.append(_DEFANGED_MARKER[:-1])
        pos = end
        if closing is not None:
            pieces.append(text[pos:closing[0]])
            pieces.append(")")
            pos = closing[1]
    pieces.append(text[pos:])
    return "".join(pieces)


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
    copies = _copies(result) if isinstance(result, str) else []
    if len(copies) == 1 and result[copies[0][0]:copies[0][1]].lower() == RUNTIME_NOTE_MARKER[:-1].lower():
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
    if not _copies(value) or _is_vouched(value, tool_name, tool_call_id):
        return value
    return neutralize_marker(value)


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
            if isinstance(part, dict) and isinstance(part.get("text"), str):
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

"""Rendering for the native Telegram activity display (``native_progress``).

Pure functions: turn the consumer's activity rows into the Rich Markdown that
``sendRichMessageDraft`` accepts.  Official evidence (Bot API "Rich Markdown
style"): Rich Markdown "can contain arbitrary HTML" and sendRichMessageDraft
additionally accepts ``<tg-thinking>``.  Only the *presentation* of the
currently visible activity lines changes here; the words are the ones the
gateway already shows today, HTML-escaped.

The layout is thoughts-first: public thoughts/commentary are the emphasized
primary text, tool actions are compact one-line secondary rows grouped between
them, and one quiet phase header counts from the turn start.

Two invariants keep the block well-formed:

* The whole thinking block is a single physical line.  A blank line would end
  a Markdown HTML block, so every newline inside a row becomes ``<br>`` (or a
  space inside a one-line tool row).
* Row text is HTML-escaped, so user/tool text can never open or close tags.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

THINKING_OPEN = "<tg-thinking>"
THINKING_CLOSE = "</tg-thinking>"

_EMOJI_ID_RE = re.compile(r"^[0-9]{1,32}$")
_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)\n?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"(?<!\\)(`+)(.+?)(?<!`)\1(?!`)")
_BOLD_RE = re.compile(r"(?<!\\)\*\*([^*\n]+)\*\*(?!\*)")
_THOUGHT_PREFIX = "\U0001F4AD "

# Row states the consumer can justify (see gateway.native_progress).
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_COMPLETED = "completed"
STATE_STOPPED = "stopped"
STATE_INFO = "info"

_STATE_LABEL = {
    STATE_RUNNING: "Running",
    STATE_SUCCEEDED: "Done",
    STATE_FAILED: "Failed",
    STATE_COMPLETED: "Completed",
    STATE_STOPPED: "Stopped",
}


@dataclass(frozen=True)
class NativeIcon:
    """An AIActions sticker resolved at runtime (never hard-coded)."""

    custom_emoji_id: str
    emoji: str


def escape_text(text: str) -> str:
    return html.escape(text or "", quote=False)


def format_elapsed(seconds: float) -> str:
    """Whole elapsed seconds; under one second is ``<1s``, never a fake ``0s``.

    The result is plain text — escape it before placing it in markup.
    """
    total = max(0, int(seconds))
    if total < 1:
        return "<1s"
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s"


def _inline_markup(text: str) -> str:
    """Escaped HTML for one activity string, keeping its visible text intact.

    * fenced code blocks (``` … ```) become ``<code>`` runs;
    * the thought-bubble italic wrapping (``_line_`` / ``*line*``) becomes
      ``<i>`` (Telegram's own MarkdownV2 path renders those markers invisibly).
    Anything else is escaped verbatim.
    """
    out: list[str] = []
    pos = 0
    for match in _FENCE_RE.finditer(text):
        out.append(_text_lines(text[pos:match.start()]))
        code = match.group(1)
        out.append("<br>".join(
            f"<code>{escape_text(line)}</code>" if line else "" for line in code.split("\n")
        ))
        pos = match.end()
    out.append(_text_lines(text[pos:]))
    return "".join(out)


def _text_lines(text: str) -> str:
    if not text:
        return ""
    rendered = []
    for line in text.split("\n"):
        stripped = line.strip()
        lead = ""
        body = stripped
        if body.startswith(_THOUGHT_PREFIX.strip()):
            lead = _THOUGHT_PREFIX
            body = body[len(_THOUGHT_PREFIX.strip()):].lstrip()
        italic = (
            len(body) > 2
            and (
                (body.startswith("*") and not body.startswith("**") and body.endswith("*"))
                or (body.startswith("_") and not body.startswith("__") and body.endswith("_"))
            )
        )
        if italic:
            rendered.append(f"{escape_text(lead)}<i>{escape_text(body[1:-1])}</i>")
        else:
            # Only balanced, single-line standard Markdown bold is interpreted.
            # Unmatched/untrusted delimiters stay escaped literal text.
            cursor = 0
            pieces = []
            for code_match in _INLINE_CODE_RE.finditer(body):
                before = body[cursor:code_match.start()]
                before_cursor = 0
                for match in _BOLD_RE.finditer(before):
                    pieces.append(escape_text(before[before_cursor:match.start()]))
                    pieces.append(f"<b>{escape_text(match.group(1))}</b>")
                    before_cursor = match.end()
                pieces.append(escape_text(before[before_cursor:]))
                pieces.append(f"<code>{escape_text(code_match.group(2))}</code>")
                cursor = code_match.end()
            after = body[cursor:]
            after_cursor = 0
            for match in _BOLD_RE.finditer(after):
                pieces.append(escape_text(after[after_cursor:match.start()]))
                pieces.append(f"<b>{escape_text(match.group(1))}</b>")
                after_cursor = match.end()
            pieces.append(escape_text(after[after_cursor:]))
            rendered.append(escape_text(lead) + "".join(pieces))
    return "<br>".join(rendered)


def _emphasized_markup(text: str) -> str:
    """Bold public commentary line by line, keeping its words and order.

    Telegram code entities cannot nest inside other entities, so inline code
    and fenced lines stay ``<code>`` runs between the bold runs, and no tag
    ever spans a ``<br>``.
    """
    out: list[str] = []
    pos = 0
    for match in _FENCE_RE.finditer(text):
        out.append(_emphasized_lines(text[pos:match.start()]))
        out.append("<br>".join(
            f"<code>{escape_text(line)}</code>" if line else ""
            for line in match.group(1).split("\n")
        ))
        pos = match.end()
    out.append(_emphasized_lines(text[pos:]))
    return "".join(out)


def _emphasized_lines(text: str) -> str:
    if not text:
        return ""
    rendered = []
    for line in text.split("\n"):
        pieces = []
        cursor = 0
        for code_match in _INLINE_CODE_RE.finditer(line):
            pieces.append(_bold_run(line[cursor:code_match.start()]))
            pieces.append(f"<code>{escape_text(code_match.group(2))}</code>")
            cursor = code_match.end()
        pieces.append(_bold_run(line[cursor:]))
        rendered.append("".join(pieces))
    return "<br>".join(rendered)


def _bold_run(text: str) -> str:
    # The whole run is emphasized, so the author's own balanced ** markers
    # are dropped rather than nested.
    plain = _BOLD_RE.sub(r"\1", text)
    if not plain.strip():
        return escape_text(plain)
    return f"<b>{escape_text(plain)}</b>"


def _one_line_markup(text: str, *, code: bool = False) -> str:
    """Escaped single-line HTML: line breaks fold to spaces, fences to code runs."""
    parts: list[str] = []
    pos = 0

    def prose(segment: str) -> None:
        for line in segment.split("\n"):
            line = line.strip()
            if line:
                parts.append(f"<code>{escape_text(line)}</code>" if code else _text_lines(line))

    for match in _FENCE_RE.finditer(text):
        prose(text[pos:match.start()])
        parts.extend(
            f"<code>{escape_text(line.strip())}</code>"
            for line in match.group(1).split("\n") if line.strip()
        )
        pos = match.end()
    prose(text[pos:])
    return " ".join(parts)


def _natural_commentary(text: str, *, add_thought_marker: bool = False) -> str:
    """Keep the recognized thought marker but remove only its legacy italics."""
    has_marker = text.startswith(_THOUGHT_PREFIX)
    body = text[len(_THOUGHT_PREFIX):] if has_marker else text
    lines = body.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if (
            len(stripped) > 2
            and ((stripped.startswith("*") and not stripped.startswith("**") and stripped.endswith("*"))
                 or (stripped.startswith("_") and not stripped.startswith("__") and stripped.endswith("_")))
        ):
            stripped = stripped[1:-1]
        result.append(stripped)
    normalized = "\n".join(result)
    if has_marker or add_thought_marker:
        return f"{_THOUGHT_PREFIX}{normalized}"
    return normalized


def _icon_tag(icon: Optional[NativeIcon]) -> str:
    if icon is None or not icon.emoji:
        return ""
    if not icon.custom_emoji_id:
        # Fallback mode: the real sticker emoji as plain text, no custom tag.
        return f"{escape_text(icon.emoji)} "
    if not _EMOJI_ID_RE.match(icon.custom_emoji_id):
        return ""
    return f'<tg-emoji emoji-id="{icon.custom_emoji_id}">{escape_text(icon.emoji)}</tg-emoji> '


def _icon_for(row: Any, icons: Optional[Mapping[str, NativeIcon]]) -> str:
    if not icons:
        return ""
    state = getattr(row, "state", STATE_INFO)
    if state == STATE_RUNNING:
        return _icon_tag(icons.get("running") or icons.get("thinking"))
    if state == STATE_SUCCEEDED:
        return _icon_tag(icons.get("succeeded"))
    if state == STATE_FAILED:
        return _icon_tag(icons.get("failed"))
    return _icon_tag(icons.get("thinking")) if getattr(row, "kind", "") != "tool" else ""


def _header_label(running: Any, icons: Optional[Mapping[str, NativeIcon]] = None) -> str:
    """Use the safe action title, not arguments or a duplicated full row."""
    if running is None:
        return "Thinking"
    tool = getattr(running, "tool", None)
    raw = str(getattr(running, "text", "") or "")
    if tool:
        try:
            from agent.display import get_tool_emoji, get_tool_verb

            emoji = get_tool_emoji(tool, default="⚙️")
            verb = get_tool_verb(tool)
        except Exception:
            emoji = ""
            verb = None
        if emoji and raw.startswith(f"{emoji} "):
            raw = raw[len(emoji) + 1:]
        title = next((line.strip() for line in raw.splitlines() if line.strip()), "")
        if verb and title.startswith(verb):
            if tool == "terminal" and verb.lower() in {"running", "executing"}:
                return f"{verb} command"
            if verb.lower() in {"running", "executing"}:
                if title.startswith(f"{verb} "):
                    return " ".join(title.split()[:2])
                return _tool_display_label(tool, STATE_RUNNING)
            return verb
        if title.lower().startswith(("running ", "executing ")):
            words = title.split()
            if len(words) > 1:
                return " ".join(words[:2])
            return _tool_display_label(tool, STATE_RUNNING)
        return title or _tool_display_label(tool)
    title = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    return title or "Working"


def _tool_display_label(tool: Optional[str], state: str = "") -> str:
    if tool:
        if state != STATE_RUNNING:
            try:
                from agent.display import get_tool_verb

                verb = get_tool_verb(tool)
            except Exception:
                verb = None
            if verb and not verb.lower().startswith(("running", "executing")):
                return verb
        # Active state is shown in its own status field. Use the tool identity,
        # not an action verb that would repeat "Running" beside that status.
        return str(tool).replace("_", " ").strip().capitalize()
    return "Tool action"


def _is_command_tool(tool: Optional[str]) -> bool:
    """Tools whose preview is a command or source text rather than prose."""
    try:
        from agent.display import get_tool_verb

        verb = get_tool_verb(tool) if tool else None
    except Exception:
        verb = None
    return bool(verb) and verb.lower().startswith(("running", "executing"))


def _tool_detail_text(text: str, label: str, tool: Optional[str]) -> tuple[str, bool]:
    """Remove only a recognized decorative emoji plus repeated opening verb.

    Returns the detail and whether the gateway's verb heading was recognized.
    """
    try:
        from agent.display import get_tool_emoji

        emoji = get_tool_emoji(tool, default="⚙️") if tool else ""
    except Exception:
        emoji = ""
    prefix = f"{emoji} " if emoji and text.startswith(f"{emoji} ") else ""
    candidate = text[len(prefix):]
    recognized = [label]
    if tool:
        try:
            from agent.display import get_tool_verb

            verb = get_tool_verb(tool)
            if tool == "terminal" and verb and verb.lower() in {"running", "executing"}:
                recognized.append(f"{verb} command")
            if verb:
                recognized.append(verb)
        except Exception:
            pass
    for heading in sorted(recognized, key=len, reverse=True):
        if candidate == heading:
            return "", True
        if candidate.startswith(heading) and len(candidate) > len(heading) and candidate[len(heading)].isspace():
            return candidate[len(heading):].lstrip(), True
    return text, False


def _tool_state_markup(row: Any, state: str, now: Optional[float]) -> str:
    """Concise state: a live timer while running, a word once it is known."""
    if state == STATE_RUNNING:
        if now is None:
            return f"<i>{_STATE_LABEL[STATE_RUNNING]}</i>"
        started = float(getattr(row, "started_at", now) or now)
        return f"<i>{escape_text(format_elapsed(now - started))}</i>"
    text = _STATE_LABEL.get(state)
    if not text:
        return ""
    duration = getattr(row, "duration", None)
    # Tiny completed calls stay quiet; raw durations live in the history.
    if state in (STATE_SUCCEEDED, STATE_FAILED) and duration is not None and duration >= 1:
        text += f" · {format_elapsed(duration)}"
    tag = "b" if state == STATE_FAILED else "i"
    return f"<{tag}>{escape_text(text)}</{tag}>"


def render_row(
    row: Any,
    icons: Optional[Mapping[str, NativeIcon]] = None,
    *,
    now: Optional[float] = None,
) -> str:
    raw_text = str(getattr(row, "text", "") or "")
    kind = getattr(row, "kind", "")
    state = getattr(row, "state", STATE_INFO)
    if kind == "tool":
        # One compact line: friendly action label, its detail, concise state.
        tool = str(getattr(row, "tool", "") or "")
        raw_label = _tool_display_label(getattr(row, "tool", None), state)
        detail, recognized = _tool_detail_text(raw_text, raw_label, tool)
        action = " ".join(filter(None, (
            escape_text(raw_label),
            _one_line_markup(detail, code=recognized and _is_command_tool(tool)),
        )))
        content = f"{_icon_for(row, icons)}{action}"
        state_markup = _tool_state_markup(row, state, now)
        if state_markup:
            content += f" · {state_markup}"
    elif kind in {"thought", "commentary"}:
        # Public updates are the primary text: their original words and
        # order, emphasized, with no section heading or blanket italics.
        content = _emphasized_markup(
            _natural_commentary(raw_text, add_thought_marker=kind == "thought")
        )
    else:
        content = f"{_icon_for(row, icons)}{_inline_markup(raw_text)}"
    return content.strip()


def render_thinking_block(
    rows: Sequence[Any],
    *,
    now: float,
    idle_since: Optional[float] = None,
    icons: Optional[Mapping[str, NativeIcon]] = None,
    turn_started_at: Optional[float] = None,
) -> str:
    """``<tg-thinking>…</tg-thinking>`` for the current rows (one physical line).

    ``turn_started_at`` is the turn's immutable origin on the same clock as
    ``now``; with it the header never restarts on a new tool or idle gap.
    Callers that do not supply it keep the per-activity origin.
    """
    running = next((r for r in reversed(rows) if getattr(r, "state", "") == STATE_RUNNING), None)
    if turn_started_at is not None:
        started = turn_started_at
    elif running is not None:
        started = float(getattr(running, "started_at", now) or now)
    elif idle_since is not None:
        started = idle_since
    else:
        started = float(getattr(rows[0], "started_at", now) or now) if rows else now
    head_icon = _icon_tag((icons or {}).get("thinking")) if icons else ""
    title = escape_text(_header_label(running, icons))
    blocks = [f"{head_icon}{title} · {escape_text(format_elapsed(now - started))}"]
    compact: list[str] = []
    for row in rows:
        rendered = render_row(row, icons, now=now)
        if not rendered:
            continue
        if getattr(row, "kind", "") in {"thought", "commentary"}:
            if compact:
                blocks.append("<br>".join(compact))
                compact = []
            blocks.append(rendered)
        else:
            compact.append(rendered)
    if compact:
        blocks.append("<br>".join(compact))
    body = "<br><br>".join(blocks)
    return f"{THINKING_OPEN}{body}{THINKING_CLOSE}"


def compose_markdown(
    rows: Sequence[Any],
    answer: str,
    *,
    now: float,
    idle_since: Optional[float] = None,
    icons: Optional[Mapping[str, NativeIcon]] = None,
    turn_started_at: Optional[float] = None,
) -> str:
    block = render_thinking_block(
        rows, now=now, idle_since=idle_since, icons=icons, turn_started_at=turn_started_at,
    )
    if block and answer:
        return f"{block}\n\n{answer}"
    return block or answer

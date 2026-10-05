"""Rendering for the native Telegram activity display (``native_progress``).

Pure functions: turn the consumer's activity rows into the Rich Markdown that
``sendRichMessageDraft`` accepts.  Official evidence (Bot API "Rich Markdown
style"): Rich Markdown "can contain arbitrary HTML" and sendRichMessageDraft
additionally accepts ``<tg-thinking>``.  Only the *presentation* of the
currently visible activity lines changes here; the line text itself is the
exact string the gateway already shows today, HTML-escaped.

Two invariants keep the block well-formed:

* The whole thinking block is a single physical line.  A blank line would end
  a Markdown HTML block, so every newline inside a row becomes ``<br>``.
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
_THOUGHT_PREFIX = "\U0001F4AD "

# Row states the consumer can justify (see gateway.native_progress).
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_COMPLETED = "completed"
STATE_STOPPED = "stopped"
STATE_INFO = "info"

_STATE_LABEL = {
    STATE_RUNNING: "Executing",
    STATE_SUCCEEDED: "Succeeded",
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
    total = max(0, int(seconds))
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
            rendered.append(escape_text(line if not lead else lead + body))
    return "<br>".join(rendered)


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


def _header_label(running: Any) -> str:
    if running is None:
        return "Thinking"
    tool = getattr(running, "tool", None)
    if tool:
        try:
            from agent.display import get_tool_verb

            verb = get_tool_verb(tool)
        except Exception:
            verb = None
        if verb:
            return verb.split()[0]
    return "Working"


def render_row(row: Any, icons: Optional[Mapping[str, NativeIcon]] = None) -> str:
    text = _inline_markup(str(getattr(row, "text", "") or ""))
    repeat = int(getattr(row, "repeat", 1) or 1)
    if repeat > 1 and getattr(row, "kind", "") == "tool":
        # The existing dedup display appends the counter to the line itself;
        # the row text already carries it, so nothing is added here.
        pass
    state = getattr(row, "state", STATE_INFO)
    label = _STATE_LABEL.get(state)
    suffix = ""
    if getattr(row, "kind", "") == "tool" and label:
        suffix = f" — {label}"
        duration = getattr(row, "duration", None)
        if duration is not None and state in (STATE_SUCCEEDED, STATE_FAILED):
            suffix += f" · {format_elapsed(duration)}"
    return f"{_icon_for(row, icons)}{text}{suffix}"


def render_thinking_block(
    rows: Sequence[Any],
    *,
    now: float,
    idle_since: Optional[float] = None,
    icons: Optional[Mapping[str, NativeIcon]] = None,
) -> str:
    """``<tg-thinking>…</tg-thinking>`` for the current rows (one physical line)."""
    if not rows:
        return ""
    running = next((r for r in reversed(rows) if getattr(r, "state", "") == STATE_RUNNING), None)
    if running is not None:
        started = float(getattr(running, "started_at", now) or now)
    elif idle_since is not None:
        started = idle_since
    else:
        started = float(getattr(rows[0], "started_at", now) or now)
    current = running if running is not None else rows[-1]
    head_icon = _icon_tag((icons or {}).get("thinking")) if icons else ""
    summary = _inline_markup(str(getattr(current, "text", "") or "")).split("<br>")[0]
    header = f"{head_icon}{escape_text(_header_label(running))} · {format_elapsed(now - started)} — {summary}"
    body = "<br>".join([header] + [render_row(r, icons) for r in rows])
    return f"{THINKING_OPEN}{body}{THINKING_CLOSE}"


def compose_markdown(
    rows: Sequence[Any],
    answer: str,
    *,
    now: float,
    idle_since: Optional[float] = None,
    icons: Optional[Mapping[str, NativeIcon]] = None,
) -> str:
    block = render_thinking_block(rows, now=now, idle_since=idle_since, icons=icons)
    if block and answer:
        return f"{block}\n\n{answer}"
    return block or answer

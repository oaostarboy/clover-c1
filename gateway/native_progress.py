"""Platform-neutral state for the native activity display.

The native Telegram display (``platforms.telegram.extra.native_progress``)
shows today's already-visible progress lines (tool progress, visible thoughts,
commentary) inside ONE ephemeral draft owned by ``GatewayStreamConsumer``.
This module holds the pure pieces:

* :class:`ActivityLedger` — the whole-turn list of rows.  Row ``text`` is the
  exact string the gateway renders today; nothing is summarised, relabelled or
  dropped.  Per-tool state (running / succeeded / failed / completed) is only
  claimed when the callback pairing makes it honest.
* :class:`NativeAwareProgressQueue` — a ``queue.Queue`` whose ``put`` hands
  today's progress items to the consumer while it owns the display, and
  otherwise behaves exactly like a normal queue.
* :class:`NativeProgressScope` — the (session, run generation) a draft belongs
  to, for Stop authorization.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_COMPLETED = "completed"
STATE_STOPPED = "stopped"
STATE_INFO = "info"


@dataclass
class ActivityRow:
    text: str
    kind: str = "line"                 # tool | thought | commentary | line
    tool: Optional[str] = None
    state: str = STATE_INFO
    started_at: float = 0.0
    duration: Optional[float] = None
    repeat: int = 1
    outstanding: int = 0               # tool starts not yet matched by a completion
    call_id: Optional[str] = None
    raw_detail: Optional[str] = None
    correlation: str = "unknown"
    aggregate_error: bool = False


class ActivityLedger:
    """Whole-turn activity rows, in the order the gateway would show them."""

    def __init__(self) -> None:
        self.rows: List[ActivityRow] = []
        self._tool_outstanding: Dict[str, int] = {}
        self._ambiguous: Dict[str, bool] = {}
        self._aggregate_error: Dict[str, bool] = {}

    def __len__(self) -> int:
        return len(self.rows)

    def add_line(self, text: str, *, kind: str = "line", tool: Optional[str] = None, now: float = 0.0, call_id: Optional[str] = None, raw_detail: Optional[str] = None) -> ActivityRow:
        if tool:
            kind = "tool"
        row = ActivityRow(
            text=text, kind=kind, tool=tool,
            state=STATE_RUNNING if tool else STATE_INFO,
            started_at=now, outstanding=1 if tool else 0,
            call_id=call_id or None, raw_detail=raw_detail,
            correlation="call_id" if call_id else "unknown",
        )
        self.rows.append(row)
        if tool:
            self._tool_outstanding[tool] = self._tool_outstanding.get(tool, 0) + 1
        return row

    def replace_last(self, text: str, *, tool: Optional[str] = None, now: float = 0.0) -> ActivityRow:
        """Dedup: today's bubble rewrites its last line to ``… (×N)``."""
        if not self.rows:
            return self.add_line(text, tool=tool, now=now)
        row = self.rows[-1]
        row.text = text
        row.repeat += 1
        if row.kind == "tool" and row.tool:
            row.outstanding += 1
            row.state = STATE_RUNNING
            row.duration = None
            self._tool_outstanding[row.tool] = self._tool_outstanding.get(row.tool, 0) + 1
        return row

    def complete_tool(self, tool: str, *, duration: Optional[float], is_error: Optional[bool], call_id: Optional[str] = None) -> None:
        if call_id:
            matches = [r for r in self.rows if r.tool == tool and r.call_id == call_id and r.outstanding]
            if len(matches) != 1:
                return  # unknown/duplicate identity is never name-correlated
            row = matches[0]
            row.outstanding = 0
            row.duration = duration
            row.state = STATE_COMPLETED if is_error is None else STATE_FAILED if is_error else STATE_SUCCEEDED
            self._tool_outstanding[tool] = max(0, self._tool_outstanding.get(tool, 0) - 1)
            return
        total = self._tool_outstanding.get(tool, 0)
        if total <= 0:
            return                      # no row was shown for this call (hidden / deduped)
        running = [r for r in self.rows if r.kind == "tool" and r.tool == tool and r.state == STATE_RUNNING]
        precise = (
            total == 1
            and not self._ambiguous.get(tool)
            and len(running) == 1
            and running[0].repeat == 1
        )
        self._tool_outstanding[tool] = total - 1
        if precise:
            row = running[0]
            row.outstanding = 0
            row.duration = duration
            if is_error is None:
                row.state, row.duration = STATE_COMPLETED, None
            else:
                row.state = STATE_FAILED if is_error else STATE_SUCCEEDED
            return
        # Several same-name calls overlap (or one row stands for several
        # calls): the callback carries no call id, so claim nothing per call.
        self._ambiguous[tool] = True
        self._aggregate_error[tool] = self._aggregate_error.get(tool, False) or is_error is True
        for row in running:
            row.aggregate_error = self._aggregate_error[tool]
            row.correlation = "aggregate_unknown"
        if self._tool_outstanding[tool] == 0:
            for row in running:
                row.outstanding = 0
                row.state = STATE_FAILED if self._aggregate_error[tool] else STATE_COMPLETED
                row.duration = None
            self._ambiguous[tool] = False
            self._aggregate_error[tool] = False

    @staticmethod
    def sanitize_detail(arguments: Any, preview: str = "") -> str:
        """Retain permitted arguments, never results; force existing secret redaction."""
        import json
        import re
        from agent.redact import redact_sensitive_text, _key_has_secret_keyword

        def safe(value):
            if isinstance(value, dict):
                return {str(k): "[REDACTED]" if _key_has_secret_keyword(str(k)) else safe(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [safe(v) for v in value]
            if isinstance(value, str):
                value = redact_sensitive_text(value, force=True)
                # Diagnostics are public UI data, not a host filesystem disclosure.
                value = re.sub(r"(?<![\w:])(?:/(?:home|Users|mnt|tmp|etc|var|root|opt)/|[A-Za-z]:[\\/])[^\s\"'<>|]*", "[host-path]", value)
                return value
            return value

        permitted = safe(arguments)
        if isinstance(permitted, dict) and set(permitted) == {"command"}:
            return str(permitted["command"])
        if permitted:
            return json.dumps(permitted, ensure_ascii=False, indent=2, default=str)
        return str(safe(preview))

    def lines(self) -> List[str]:
        return [row.text for row in self.rows]

    def snapshot(self) -> List[ActivityRow]:
        return [ActivityRow(**vars(row)) for row in self.rows]


@dataclass(frozen=True)
class NativeProgressScope:
    """Which run a native draft belongs to (captured at bind time)."""

    session_key: str
    run_generation: Optional[int]
    source: Any = None


class NativeAwareProgressQueue(queue.Queue):
    """``queue.Queue`` that lets the native composer take today's progress items.

    ``holder`` is the gateway's ``stream_consumer_holder`` list.  While the
    consumer reports ``native_activity_active`` its ``route_progress_item``
    consumes the item (the exact string the legacy sender would have shown);
    in every other case — feature off, unsupported route, fallback — this is a
    plain ``queue.Queue`` and the legacy sender behaves exactly as before.
    """

    def __init__(self, holder: List[Any]) -> None:
        super().__init__()
        self._holder = holder

    def put(self, item: Any, block: bool = True, timeout: Optional[float] = None) -> None:
        consumer = self._holder[0] if self._holder else None
        if consumer is not None and getattr(consumer, "owns_progress_routing", False) is True:
            try:
                if consumer.route_progress_item(item):
                    return
            except Exception:
                pass
        super().put(item, block, timeout)

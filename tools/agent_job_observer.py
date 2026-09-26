"""Observe explicitly registered external agent CLI jobs (Claude, Codex, Clover).

A background ``terminal`` process becomes an *agent job* only when the caller
passes ``agent_job={"title": ..., "model": ..., "parser": ...}`` at spawn time.
Nothing attaches to pre-existing or unregistered processes, and nothing is
inferred from the command line.

The observer hangs off the existing process registry (``ProcessSession``):
``_emit_output`` feeds it every output chunk and ``_move_to_finished`` reports
the exit. It translates what it can *truthfully* observe into the same
``subagent.*`` events in-process delegate children produce, delivered to the
activity sink (the gateway turn's ``DelegationActivityPublisher``) that was
current when the job was registered — so it lands only in the chat, thread
and profile that launched it.

Parsers (bounded, fail-safe; unknown or malformed lines are ignored):

* ``claude-stream-json`` — ``claude -p --output-format stream-json --verbose``.
  ``tool_use`` → tool start, ``tool_result`` → outcome (never its content),
  assistant ``text`` blocks → public notes. ``thinking`` / ``redacted_thinking``
  blocks are never read. The final ``result`` text becomes the finding.
* ``clover-activity`` — ``clover -z … --activity-events``: versioned JSONL
  lines (``{"clover_activity": 1, "event": …}``) the Clover CLI writes to
  stderr (see ``clover_cli/activity_events.py``). The final stdout answer is
  plain text and is never parsed.
* ``none`` — lifecycle only: start, exit and output growth. The card says
  "lifecycle only (no tool visibility)"; output text is never shown.

All free text still passes the tracker's redact-then-truncate path.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

SUPPORTED_PARSERS = ("claude-stream-json", "clover-activity", "none")
CLOVER_ACTIVITY_VERSION = 1

# A single structured line larger than this is dropped (e.g. a tool_result
# carrying a whole file) — the buffer never grows without bound.
_MAX_LINE_CHARS = 1_000_000
_TITLE_MAX = 60


def validate_agent_job_spec(spec: Any) -> tuple[Optional[Dict[str, str]], Optional[str]]:
    """Return ``(normalized_spec, error)`` for a terminal ``agent_job`` value."""
    if not isinstance(spec, dict):
        return None, "agent_job must be an object: {title, model?, parser?}"
    title = str(spec.get("title") or "").strip()
    if not title:
        return None, "agent_job.title is required (a short label shown to the user)"
    parser = str(spec.get("parser") or "none").strip().lower()
    if parser not in SUPPORTED_PARSERS:
        return None, (
            f"agent_job.parser must be one of {', '.join(SUPPORTED_PARSERS)} "
            f"(got {parser!r})"
        )
    return {
        "title": title[:200],
        "model": str(spec.get("model") or "").strip()[:80],
        "parser": parser,
    }, None


class AgentJobObserver:
    """Per-process observer. Thread-safe; never raises into the reader thread."""

    def __init__(
        self,
        *,
        session_id: str,
        sink: Any,
        group_id: str,
        index: int,
        title: str,
        model: str = "",
        parser: str = "none",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_id = session_id
        self.group_id = group_id
        self.index = index
        self.title = title
        self.model = model
        self.parser = parser if parser in SUPPORTED_PARSERS else "none"
        self.visibility = "lifecycle" if self.parser == "none" else "tools"
        self._sink = sink
        self._clock = clock
        self._lock = threading.Lock()
        self._buf = ""
        self._skipping = False
        self._tool_names: Dict[str, str] = {}
        self._result_text: Optional[str] = None
        self._result_is_error: Optional[bool] = None
        self._finished = False
        self.started_at = clock()
        self.last_output_at = self.started_at
        self.malformed_lines = 0
        self.dropped_lines = 0

    # -- emission ---------------------------------------------------------

    def _emit(self, event_type: str, tool_name: Any = None, preview: Any = None,
              args: Any = None, **kw: Any) -> None:
        kw.update(
            delegation_id=self.group_id,
            subagent_id=self.session_id,
            task_index=self.index,
            title=self.title,
            provider="external CLI",
            visibility=self.visibility,
        )
        if self.model:
            kw["model"] = self.model
        try:
            self._sink.observe(event_type, tool_name, preview, args, **kw)
        except Exception:
            logger.debug("agent job %s: sink observe failed", self.session_id, exc_info=True)

    def start(self) -> None:
        self._emit("subagent.start", preview=self.title)

    # -- output -----------------------------------------------------------

    def feed(self, chunk: str) -> None:
        """Consume one raw output chunk (called from the registry reader)."""
        if not chunk:
            return
        lines = []
        with self._lock:
            if self._finished:
                return
            self.last_output_at = self._clock()
            if self.parser == "none":
                return  # lifecycle only: output growth is the whole signal
            data = self._buf + chunk
            parts = data.split("\n")
            self._buf = parts.pop()
            for part in parts:
                if self._skipping:
                    # Tail of an oversized line: drop through its newline.
                    self._skipping = False
                    continue
                lines.append(part)
            if len(self._buf) > _MAX_LINE_CHARS:
                self._buf = ""
                self._skipping = True
                self.dropped_lines += 1
        for line in lines:
            self._parse_line(line)

    def _parse_line(self, line: str) -> None:
        text = line.strip()
        if len(text) > _MAX_LINE_CHARS:
            with self._lock:
                self.dropped_lines += 1
            return
        if not text.startswith("{"):
            return  # non-structured output is never surfaced
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError):
            with self._lock:
                self.malformed_lines += 1
            return
        if not isinstance(obj, dict):
            return
        try:
            if self.parser == "claude-stream-json":
                self._parse_claude(obj)
            elif self.parser == "clover-activity":
                self._parse_clover(obj)
        except Exception:
            with self._lock:
                self.malformed_lines += 1
            logger.debug("agent job %s: parse failed", self.session_id, exc_info=True)

    def _parse_claude(self, obj: Dict[str, Any]) -> None:
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            model = obj.get("model")
            if isinstance(model, str) and model and not self.model:
                self.model = model[:80]
                self._emit("subagent.progress")  # identity refresh, no state
            return
        if kind == "assistant":
            message = obj.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    call_id = str(block.get("id") or "")
                    name = str(block.get("name") or "tool")
                    if call_id:
                        self._tool_names[call_id] = name
                    tool_input = block.get("input")
                    self._emit(
                        "subagent.tool", name, None,
                        tool_input if isinstance(tool_input, dict) else None,
                        tool_call_id=call_id or None,
                    )
                elif btype == "text":
                    # Intentional assistant text only; thinking blocks are a
                    # different type and never read.
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        self._emit("subagent.thinking", preview=text, note_kind="note")
            return
        if kind == "user":
            message = obj.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id") or "")
                self._emit(
                    "subagent.tool_done",
                    self._tool_names.pop(call_id, None),
                    tool_call_id=call_id or None,
                    is_error=bool(block.get("is_error")),
                )
            return
        if kind == "result":
            result = obj.get("result")
            with self._lock:
                self._result_text = result if isinstance(result, str) else None
                self._result_is_error = bool(obj.get("is_error"))

    def _parse_clover(self, obj: Dict[str, Any]) -> None:
        if obj.get("clover_activity") != CLOVER_ACTIVITY_VERSION:
            return
        event = obj.get("event")
        if event == "start":
            model = obj.get("model")
            if isinstance(model, str) and model and not self.model:
                self.model = model[:80]
                self._emit("subagent.progress")
        elif event == "tool.started":
            self._emit(
                "subagent.tool", obj.get("tool") or "tool", obj.get("summary"), None,
                tool_call_id=obj.get("call_id") or None,
            )
        elif event == "tool.completed":
            self._emit(
                "subagent.tool_done", obj.get("tool"),
                tool_call_id=obj.get("call_id") or None,
                duration_seconds=obj.get("duration"),
                is_error=bool(obj.get("is_error")),
            )
        elif event == "note":
            text = obj.get("text")
            if isinstance(text, str) and text.strip():
                self._emit("subagent.thinking", preview=text, note_kind="note")
        elif event == "result":
            text = obj.get("text")
            with self._lock:
                self._result_text = text if isinstance(text, str) else None
                self._result_is_error = obj.get("status") not in (None, "completed")

    # -- exit ---------------------------------------------------------------

    def finish(self, exit_code: Optional[int], completion_reason: str = "exited") -> None:
        with self._lock:
            if self._finished:
                return
            tail = self._buf if not self._skipping else ""
            self._buf = ""
        if tail:
            self._parse_line(tail)
        with self._lock:
            self._finished = True
            result_text = self._result_text
            result_error = self._result_is_error
        duration = max(0.0, self._clock() - self.started_at)
        if completion_reason == "killed":
            self._emit("subagent.complete", status="interrupted", duration_seconds=duration,
                       reason="stopped (process killed)")
            return
        if completion_reason in {"lost", "failed_start"}:
            self._emit("subagent.complete", status="failed", duration_seconds=duration,
                       summary=f"process {completion_reason.replace('_', ' ')}")
            return
        if exit_code == 0 and not result_error:
            self._emit("subagent.complete", status="completed", duration_seconds=duration,
                       summary=result_text or "")
            return
        detail = f"exited with code {exit_code}" if exit_code not in (None, 0) else "reported an error"
        if result_error and result_text:
            detail = f"{detail}: {result_text}"
        self._emit("subagent.complete", status="failed", duration_seconds=duration,
                   summary=detail)

    def liveness(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "registered": not self._finished,
                "external": True,
                "seconds_since_activity": round(self._clock() - self.last_output_at, 1),
                "current_tool": None,
                "activity": "producing output",
            }


def register_agent_job(session: Any, spec: Dict[str, str], *, sink: Any = None) -> Dict[str, Any]:
    """Attach an observer to a freshly spawned background process.

    ``sink`` defaults to the activity sink bound to the current gateway turn.
    Without one (plain CLI, disabled display, non-gateway caller) nothing is
    attached and the result says so — the job still runs normally.
    """
    if sink is None:
        from agent.delegation_activity import current_activity_sink

        sink = current_activity_sink()
    if sink is None or not hasattr(sink, "external_job_identity"):
        return {
            "observed": False,
            "reason": "no live activity surface in this session "
            "(delegation activity is off here or this is not a gateway chat)",
        }
    group_id, index = sink.external_job_identity()
    observer = AgentJobObserver(
        session_id=session.id,
        sink=sink,
        group_id=group_id,
        index=index,
        title=spec["title"],
        model=spec.get("model", ""),
        parser=spec.get("parser", "none"),
    )
    with session._lock:
        session.agent_job = observer
        backlog = session.output_buffer
    observer.start()
    if backlog:
        # Output produced between spawn and attach. Duplicates from a racing
        # reader chunk are harmless: tool calls dedupe by id, notes by text.
        observer.feed(backlog)
    if session.exited:
        observer.finish(session.exit_code, session.completion_reason)
    return {
        "observed": True,
        "title": observer.title,
        "parser": observer.parser,
        "visibility": observer.visibility,
    }


def agent_job_liveness(session_id: str) -> Dict[str, Any]:
    """Liveness for an external job id (``proc_…``), for the heartbeat probe."""
    try:
        from tools.process_registry import process_registry

        session = process_registry.get(session_id)
    except Exception:
        return {}
    observer = getattr(session, "agent_job", None) if session is not None else None
    if observer is None:
        return {"registered": False, "external": True}
    return observer.liveness()

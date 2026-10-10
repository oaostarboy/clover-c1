"""Structured activity events for Clover CLI workers (``clover -z --activity-events``).

When a parent Clover session runs another Clover agent as an external worker
(for example a Luna profile: ``clover -p luna -z "…" --activity-events``) and
registers it with ``terminal(background=true, agent_job={"parser":
"clover-activity", …})``, the parent's chat can show the worker's real tool
calls and public progress notes instead of just "running".

Wire format — one JSON object per line on **stderr** (stdout keeps carrying
only the final answer, so existing pipelines are unaffected)::

    {"clover_activity": 1, "event": "start", "model": "…"}
    {"clover_activity": 1, "event": "tool.started", "tool": "terminal", "summary": "pytest -q"}
    {"clover_activity": 1, "event": "tool.completed", "tool": "terminal", "duration": 2.1, "is_error": false}
    {"clover_activity": 1, "event": "note", "text": "Checking the scheduler lock next."}
    {"clover_activity": 1, "event": "result", "status": "completed", "text": "…"}

Guarantees: summaries and notes are redacted then truncated before they are
written; notes come only from the agent's *visible* interim commentary
(``interim_assistant_callback``, reasoning blocks already stripped, plus a
second block strip here) — reasoning events are never written; tool output is
never written, only the outcome. Consumers must ignore unknown events and
unknown versions (``tools/agent_job_observer.py`` does).
"""

from __future__ import annotations

import json
import threading
from typing import Any, Optional, TextIO

ACTIVITY_EVENTS_VERSION = 1
_SUMMARY_MAX = 120
_NOTE_MAX = 200
_RESULT_MAX = 600


class ActivityEventWriter:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._broken = False
        self._started = False

    def _write(self, event: str, **fields: Any) -> None:
        if self._broken:
            return
        payload = {"clover_activity": ACTIVITY_EVENTS_VERSION, "event": event}
        payload.update({k: v for k, v in fields.items() if v is not None})
        try:
            line = json.dumps(payload, ensure_ascii=False)
            with self._lock:
                self._stream.write(line + "\n")
                self._stream.flush()
        except Exception:
            # A closed pipe must never take the worker down with it.
            self._broken = True

    def start(self, model: Optional[str]) -> None:
        from agent.delegation_activity import sanitize_text

        if self._started:
            return  # one start per run, even if the agent is rebuilt mid-run
        self._started = True
        self._write("start", model=sanitize_text(model, 80) or None)

    def tool_progress_callback(self, event_type: str, tool_name: Any = None,
                               preview: Any = None, args: Any = None, **kw: Any) -> None:
        from agent.delegation_activity import sanitize_text, summarize_tool_call, PUBLIC_ACTIVITY_STATUS

        if event_type == "agent.status":
            status = kw.get("activity_status")
            if status in PUBLIC_ACTIVITY_STATUS:
                self._write("status", status=status)
            return
        if not tool_name or tool_name == "_thinking":
            return  # scratch / reasoning relays are never written
        if event_type == "tool.started":
            self._write(
                "tool.started",
                tool=sanitize_text(tool_name, 40),
                summary=summarize_tool_call(tool_name, preview, args, limit=_SUMMARY_MAX) or None,
                call_id=str(kw["tool_call_id"]) if kw.get("tool_call_id") else None,
            )
        elif event_type == "tool.completed":
            duration = kw.get("duration")
            self._write(
                "tool.completed",
                tool=sanitize_text(tool_name, 40),
                duration=round(float(duration), 2) if isinstance(duration, (int, float)) else None,
                is_error=bool(kw.get("is_error")),
                call_id=str(kw["tool_call_id"]) if kw.get("tool_call_id") else None,
            )

    def interim_callback(self, text: Any, already_streamed: bool = False) -> None:
        from agent.delegation_activity import extract_progress_note, sanitize_text

        note = sanitize_text(extract_progress_note(text), _NOTE_MAX)
        if note:
            self._write("note", text=note)

    def result(self, text: Any, status: str) -> None:
        from agent.delegation_activity import extract_progress_note, sanitize_text

        self._write(
            "result",
            status=status,
            text=sanitize_text(extract_progress_note(text), _RESULT_MAX) or None,
        )

    def model_fallback(self, from_model: Any = None, from_provider: Any = None,
                        to_model: Any = None, to_provider: Any = None,
                        reason: Any = None) -> None:
        from agent.delegation_activity import sanitize_text

        self._write(
            "model.fallback",
            **{
                "from": sanitize_text(from_model, 80) or None,
                "from_provider": sanitize_text(from_provider, 40) or None,
                "to": sanitize_text(to_model, 80) or None,
                "to_provider": sanitize_text(to_provider, 40) or None,
                "reason": sanitize_text(reason, 40) or None,
            },
        )


def result_status(response: Any, result: Any, failure: Optional[BaseException] = None) -> str:
    """Terminal status for the ``result`` event (shared by ``-z`` and ``chat -q``).

    An interrupted conversation can have produced side effects before the
    interruption, so it is never reported as a clean failure or completion.
    """
    result = result if isinstance(result, dict) else {}
    if failure is not None:
        return "failed"
    if result.get("interrupted") is True:
        return "interrupted_possible_effects"
    ok = bool(str(response or "").strip()) and not result.get("failed")
    if not ok:
        return "failed"
    from agent.step_continuation import needs_continuation

    # Still stopped early after every allowed resume: say so, so the parent's
    # card shows "unfinished" instead of a false "done".
    if needs_continuation(result) is not None:
        return "incomplete"
    if result.get("completed") is False:
        return "built_unverified"
    return "completed"


def attach_to_agent(agent: Any, writer: "ActivityEventWriter", *, chain: bool) -> None:
    """Route ``agent``'s tool/progress/fallback callbacks into ``writer``.

    ``chain=False`` replaces them (quiet single-query: the human renderers
    were deliberately removed). ``chain=True`` tees, so an existing human
    renderer keeps working alongside the structured stream.
    """

    def _tee(existing: Any, ours: Any) -> Any:
        if not chain or existing is None:
            return ours

        def both(*args: Any, **kwargs: Any) -> None:
            try:
                existing(*args, **kwargs)
            finally:
                ours(*args, **kwargs)

        both._clover_activity_tee = True  # type: ignore[attr-defined]
        return both

    for attr, ours in (
        ("tool_progress_callback", writer.tool_progress_callback),
        ("interim_assistant_callback", writer.interim_callback),
        ("model_fallback_callback", writer.model_fallback),
    ):
        existing = getattr(agent, attr, None)
        if getattr(existing, "_clover_activity_tee", False):
            continue  # already attached (agent re-used across turns)
        setattr(agent, attr, _tee(existing, ours))

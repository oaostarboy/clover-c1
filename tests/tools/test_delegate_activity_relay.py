"""delegate_task relays titled, grouped, outcome-bearing child activity.

Drives the real ``delegate_task`` dispatch (child construction, live
transcript wrapping, queued announcement, ``_run_single_child``) with a fake
AIAgent that emits genuine child callback events, and asserts what reaches
the parent's ``tool_progress_callback`` — the input every progress surface
consumes.
"""

import json
import threading
from unittest.mock import MagicMock, patch

from tools.delegate_tool import delegate_task


def _parent(events):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "telegram"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent._delegate_spinner = None
    parent.thinking_callback = None
    parent.session_estimated_cost_usd = 0.0
    parent.session_cost_status = "unknown"
    parent.session_cost_source = "none"

    def _record(event_type, tool_name=None, preview=None, args=None, **kw):
        events.append((event_type, tool_name, preview, dict(kw)))

    parent.tool_progress_callback = _record
    return parent


def _fake_agent_factory():
    def _make(*_a, **kw):
        child = MagicMock()
        child.model = kw.get("model") or "claude-opus-5-5"
        child.session_prompt_tokens = 10
        child.session_completion_tokens = 5
        child.session_estimated_cost_usd = 0.0
        child.tool_progress_callback = kw.get("tool_progress_callback")

        def _run(*_ra, **_rk):
            cb = child.tool_progress_callback
            cb("tool.started", "read_file", "a.py", {"path": "a.py"})
            cb("tool.completed", "read_file", None, None, duration=0.2,
               is_error=False, result="SECRET FILE BODY")
            cb("_thinking", "Reading the scheduler next")
            return {
                "final_response": "Done: fixed the bug.",
                "completed": True,
                "interrupted": False,
                "api_calls": 2,
                "messages": [],
            }

        child.run_conversation.side_effect = _run
        return child

    return _make


def test_delegate_task_relays_group_title_queue_and_tool_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    events = []
    parent = _parent(events)
    with patch("run_agent.AIAgent", side_effect=_fake_agent_factory()):
        result = json.loads(
            delegate_task(
                tasks=[
                    {"goal": "Investigate the scheduler tick lock contention",
                     "title": "Scheduler lock audit"},
                    {"goal": "Investigate why cron catchup skips jobs after DST."},
                ],
                parent_agent=parent,
            )
        )
    assert len(result["results"]) == 2

    subagent_events = [e for e in events if str(e[0]).startswith("subagent.")]
    by_type = {}
    for et, _tn, _pv, kw in subagent_events:
        by_type.setdefault(et, []).append(kw)

    # Every child is announced as queued before it starts.
    assert len(by_type["subagent.queued"]) == 2
    first_start = next(i for i, e in enumerate(subagent_events) if e[0] == "subagent.start")
    queued_idx = [i for i, e in enumerate(subagent_events) if e[0] == "subagent.queued"]
    assert max(queued_idx) < first_start

    # One delegation group id, carried on every child event.
    group_ids = {kw.get("delegation_id") for kw in (k for _e, _t, _p, k in subagent_events)}
    assert len(group_ids) == 1 and None not in group_ids

    # Titles: explicit one kept, missing one derived from the goal.
    titles = {kw["task_index"]: kw["title"] for kw in by_type["subagent.start"]}
    assert titles[0] == "Scheduler lock audit"
    assert titles[1].startswith("Investigate why cron catchup")
    assert all(kw.get("provider") for kw in by_type["subagent.start"])

    # Tool outcome is relayed; tool OUTPUT is not.
    done = by_type["subagent.tool_done"]
    assert len(done) == 2 and all(kw["is_error"] is False for kw in done)
    assert "SECRET FILE BODY" not in repr(subagent_events)

    # Visible interim content is marked as a user-facing note.
    thinking = by_type["subagent.thinking"]
    assert thinking and all(kw["note_kind"] == "note" for kw in thinking)


def test_reasoning_and_spinner_text_are_marked_not_notes():
    from types import SimpleNamespace

    from tools.delegate_tool import _build_child_progress_callback

    events = []
    parent = SimpleNamespace(
        tool_progress_callback=lambda et, tn=None, pv=None, a=None, **kw: events.append((et, kw)),
        _delegate_spinner=None,
    )
    cb = _build_child_progress_callback(0, "goal", parent, 1, subagent_id="sa-x")
    cb("reasoning.available", "_thinking", "private reasoning", None)
    cb("_thinking", "(◕‿◕) pondering...", spinner=True)
    cb("_thinking", "Checking the lock file")
    kinds = [kw["note_kind"] for et, kw in events if et == "subagent.thinking"]
    assert kinds == ["reasoning", "status", "note"]

"""Provider wait/request/retry must not hide a worker's known current action.

Every model call relays ``requesting`` -> ``waiting`` -> ``provider_result``
(PUBLIC_ACTIVITY_STATUS). Before C1.3 (f) each one overwrote the card's
doing-slot, so for most of a worker's life the card read "waiting for
provider" instead of the last tool or progress note. Real tracker + renderer.
"""

from __future__ import annotations

from agent.delegation_activity import DelegationActivityTracker

GROUP = "g1"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _ident(index=0, sid="w1", title="Audit the scheduler"):
    return dict(delegation_id=GROUP, subagent_id=sid, task_index=index, task_count=1,
                title=title, model="claude-opus-5-5")


def _tracker():
    clock = _Clock()
    return DelegationActivityTracker(clock=clock, wall_clock=clock), clock


def _start(t, **ident):
    t.observe("subagent.start", None, None, None, **(ident or _ident()))


def _tool(t, name, summary, call_id, **ident):
    ident = ident or _ident()
    t.observe("subagent.tool", name, summary, None, tool_call_id=call_id, **ident)
    t.observe("subagent.tool_done", name, None, None, tool_call_id=call_id,
              duration_seconds=0.4, **ident)


def _status(t, status, **ident):
    t.observe("subagent.progress", None, None, None, activity_status=status,
              **(ident or _ident()))


def test_provider_wait_after_a_tool_keeps_showing_the_tool():
    t, _ = _tracker()
    _start(t)
    _tool(t, "terminal", "git log", "c1")
    for status in ("requesting", "waiting"):
        _status(t, status)
        card = t.render(GROUP)
        assert "git log" in card, card
        assert not card.splitlines()[-1].lstrip("> ").startswith("⌛"), card
    # The tracker still knows the honest state for consumers.
    assert t.snapshot(GROUP)[0]["state"] == "waiting"


def test_provider_status_fills_the_slot_only_before_any_action():
    t, _ = _tracker()
    _start(t)
    _status(t, "waiting")
    assert "waiting for provider" in t.render(GROUP)


def test_retrying_stays_visible_without_hiding_the_last_action():
    t, _ = _tracker()
    _start(t)
    _tool(t, "terminal", "git log", "c1")
    _status(t, "retrying")
    card = t.render(GROUP)
    assert "git log" in card and "retrying provider" in card, card


def test_latest_progress_note_wins_over_provider_wait():
    t, _ = _tracker()
    _start(t)
    _tool(t, "terminal", "git log", "c1")
    t.observe("subagent.thinking", None, "Checking the lock next.", None,
              note_kind="note", **_ident())
    _status(t, "waiting")
    card = t.render(GROUP)
    assert "Checking the lock next." in card, card
    _status(t, "provider_result")
    assert "Checking the lock next." in t.render(GROUP)


def test_multi_worker_rows_show_last_action_not_provider_wait():
    t, _ = _tracker()
    a = dict(_ident(0, "w1", "Audit the scheduler"), task_count=2)
    b = dict(_ident(1, "w2", "Fix DST catchup"), task_count=2)
    _start(t, **a)
    _start(t, **b)
    _tool(t, "read_file", "cron/jobs.py", "c1", **a)
    _tool(t, "terminal", "pytest tests/cron -q", "c2", **b)
    _status(t, "waiting", **a)
    _status(t, "waiting", **b)
    card = t.render(GROUP)
    assert "jobs.py" in card and "pytest tests/cron -q" in card, card

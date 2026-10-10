"""Workers keep going until done: out of steps, or quit with steps left."""

from __future__ import annotations

from agent import step_continuation as sc


def _msgs(prior=None, tools=1):
    m = list(prior or [])
    m.append({"role": "user", "content": "x"})
    for i in range(tools):
        m.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{len(m)}"}]})
        m.append({"role": "tool", "content": "ok"})
    return m


def _res(text, reason, prior=None, tools=1):
    return {"final_response": text, "turn_exit_reason": reason, "api_calls": 1,
            "messages": _msgs(prior, tools), "completed": reason.startswith("text")}


DONE = "text_response(finish_reason=stop)"
OUT = "max_iterations_reached(5/5)"


class _Agent:
    class _B:
        def __init__(self, n, used=0):
            self.max_total, self.remaining = n, n - used

    def __init__(self, remaining=3):
        self.iteration_budget = self._B(5, 5 - remaining)
        self.max_iterations = 5


def test_admits_unfinished_phrases():
    yes = [
        "I didn't finish the requested wave.",
        "This is not complete: I haven't updated the tests.",
        "I did not run the required pytest set.",
        "U10 remains incomplete.",
        "the suite is not fully verified",
        "I can't honestly report this as a completed wave.",
        "this unit's scope is not fully integrated or verified yet.",
        "Remaining callers need follow-up edits before the wave is complete.",
    ]
    no = [
        "All done. 52/52 tests pass.",
        "Removed the incomplete-upload handler; tests pass.",
        "Fixed the flaky test; verified on Linux.",
    ]
    assert all(sc.admits_unfinished(t) for t in yes)
    assert not any(sc.admits_unfinished(t) for t in no)


def test_needs_continuation_kinds():
    a = _Agent(remaining=3)
    assert sc.needs_continuation(_res("partial", OUT), a) == "budget"
    assert sc.needs_continuation(_res("I didn't finish", DONE), a) == "unfinished"
    assert sc.needs_continuation(_res("all done", DONE), a) is None
    # no steps left → not an "unfinished" nudge (budget path handles it)
    assert sc.needs_continuation(_res("I didn't finish", DONE), _Agent(remaining=0)) is None
    # interrupted/failed never continue
    r = _res("partial", OUT); r["interrupted"] = True
    assert sc.needs_continuation(r, a) is None


def test_unfinished_gets_one_nudge_then_finishes():
    a = _Agent()
    calls = []
    first = _res("I didn't finish; tests not run.", DONE)

    def run(msg, hist):
        calls.append(msg)
        return _res("Done. All tests pass.", DONE, prior=hist)

    out = sc.continue_until_done(a, first, limit=2, run=run)
    assert calls == [sc.UNFINISHED_NUDGE_MESSAGE]
    assert out["final_response"] == "Done. All tests pass."
    assert out["continuation_kinds"] == ["unfinished"]


def test_unfinished_nudged_only_once():
    a = _Agent()
    calls = []

    def run(msg, hist):
        calls.append(msg)
        return _res("Still blocked: I didn't finish because the API key is missing.", DONE, prior=hist)

    out = sc.continue_until_done(a, _res("I didn't finish", DONE), limit=5, run=run)
    assert len(calls) == 1
    assert out["auto_continuations"] == 1


def test_budget_then_unfinished_then_done():
    a = _Agent()
    seq = iter([
        lambda h: _res("I didn't finish the last file", DONE, prior=h),
        lambda h: _res("All done", DONE, prior=h),
    ])
    out = sc.continue_until_done(a, _res("partial", OUT), limit=3, run=lambda m, h: next(seq)(h))
    assert out["continuation_kinds"] == ["budget", "unfinished"]
    assert out["final_response"] == "All done"


def test_no_progress_stops():
    a = _Agent()
    calls = []

    def run(msg, hist):
        calls.append(msg)
        return _res("partial", OUT, prior=hist, tools=0)

    sc.continue_until_done(a, _res("partial", OUT), limit=5, run=run)
    assert len(calls) == 1


def test_limit_zero_disables():
    out = sc.continue_until_done(_Agent(), _res("partial", OUT), limit=0, run=lambda m, h: 1 / 0)
    assert out["auto_continuations"] == 0


def test_oneshot_wires_auto_continue():
    import inspect

    from clover_cli import oneshot

    src = inspect.getsource(oneshot._run_agent)
    assert "continue_until_done" in src and "needs_continuation" in src
    # The -z / chat -q result status is shared (clover_cli.activity_events):
    # a run that stopped on its step budget reports "incomplete", not "completed".
    from clover_cli.activity_events import result_status

    assert result_status("partial", _res("partial", OUT)) == "incomplete"
    assert result_status("done", _res("done", DONE)) == "completed"


def test_observer_maps_clover_incomplete_to_unfinished():
    from tools.agent_job_observer import AgentJobObserver  # noqa: F401
    import inspect
    import tools.agent_job_observer as ajo

    assert '"incomplete"' in inspect.getsource(ajo)

"""A CLI worker that exits 0 ends on a terminal card state, never a stuck ⏳.

Real subprocesses through the real path (terminal_tool -> process registry ->
AgentJobObserver -> DelegationActivityPublisher -> Telegram-shaped adapter),
one worker per parser, each exiting 0:

* a worker that reported a verified result -> done mark, summary shown;
* a worker whose success the observer cannot verify (``none`` parser, or a
  ``clover-activity`` stream that never wrote a ``result`` record, or a
  successful parser envelope with only prose and no task verdict) -> an
  explicit "exited, result not verified" end state. Not the done mark (that
  would be a false success), and not ⏳ / "ran out of steps" (it did neither).
* a worker that really hit its turn cap uses the skin's incomplete mark and
  keeps "ran out of steps" wording, distinct from an unverified result.
"""

from __future__ import annotations

import contextvars
import json
import textwrap

import pytest

from agent.clover_flavor import outcome_mark
from agent.delegation_activity import bind_activity_sink
from tests.gateway.test_delegation_activity import FakeTelegramAdapter, _make_publisher
from tests.tools.test_agent_job_observer import (  # noqa: F401  (fixture + helpers)
    _script,
    _spawn,
    _wait_finished,
    hermetic_terminal,
)

CLAUDE_VERIFIED = textwrap.dedent("""
    import json
    print(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5"}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "Result: done\\nAll done: 3 files changed."}), flush=True)
""")
CLAUDE_TURN_CAP = textwrap.dedent("""
    import json
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                      "result": "Stopped at the turn cap."}), flush=True)
""")
CLOVER_VERIFIED = textwrap.dedent("""
    import json, sys
    def ev(**k):
        k["clover_activity"] = 1
        sys.stderr.write(json.dumps(k) + "\\n"); sys.stderr.flush()
    ev(event="start", model="luna-large")
    ev(event="result", status="completed", text="Result: done\\nAll done: 3 files changed.")
    print("All done: 3 files changed.")
""")
# Same successful parser envelopes, but without authoritative task verdicts.
CLAUDE_PROSE_ONLY = CLAUDE_VERIFIED.replace("Result: done\\n", "")
CLOVER_PROSE_ONLY = CLOVER_VERIFIED.replace("Result: done\\n", "")
CLOVER_NO_RESULT = textwrap.dedent("""
    import json, sys
    sys.stderr.write(json.dumps({"clover_activity": 1, "event": "start", "model": "luna-large"}) + "\\n")
    print("All done.")
""")
LIFECYCLE_ONLY = "print('All done: 3 files changed.')\n"


async def _run_worker(hermetic_terminal, name, body, parser):  # noqa: F811
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    ctx = contextvars.copy_context()
    ctx.run(bind_activity_sink, pub)
    spawned = ctx.run(
        _spawn, _script(hermetic_terminal, f"{name}.py", body),
        {"title": "Build the thing", "model": "luna-large", "parser": parser},
    )
    assert spawned["agent_job"]["observed"] is True, spawned
    group_id = pub._jobs_group
    await _wait_finished(pub, group_id)
    snap = pub.tracker.snapshot(group_id)[0]
    final = adapter.summary()
    await pub.aclose()
    return snap, final


@pytest.mark.asyncio
@pytest.mark.parametrize("name,body,parser", [
    ("claude_verified", CLAUDE_VERIFIED, "claude-stream-json"),
    ("clover_verified", CLOVER_VERIFIED, "clover-activity"),
])
async def test_verified_success_ends_on_the_done_mark(hermetic_terminal, name, body, parser):  # noqa: F811
    snap, final = await _run_worker(hermetic_terminal, name, body, parser)
    assert snap["state"] == "completed", snap
    assert final.startswith("✅"), final
    assert "All done: 3 files changed." in final


@pytest.mark.asyncio
@pytest.mark.parametrize("name,body,parser", [
    ("lifecycle_only", LIFECYCLE_ONLY, "none"),
    ("clover_no_result", CLOVER_NO_RESULT, "clover-activity"),
    ("claude_prose_only", CLAUDE_PROSE_ONLY, "claude-stream-json"),
    ("clover_prose_only", CLOVER_PROSE_ONLY, "clover-activity"),
])
async def test_unverifiable_clean_exit_says_so_instead_of_hourglass(
    hermetic_terminal, name, body, parser  # noqa: F811
):
    snap, final = await _run_worker(hermetic_terminal, name, body, parser)
    head = final.splitlines()[0]
    # Terminal, and never a false success.
    assert snap["state"] == "incomplete", snap
    assert not head.startswith("✅"), final
    # Not the "still waiting" hourglass, and not the turn-cap wording.
    assert not head.startswith("⏳"), final
    assert "ran out of steps" not in final, final
    assert head.startswith(outcome_mark("unverified", "☘️")), final
    assert not head.startswith(outcome_mark("done", "✅")), final
    assert "exited" in final and "not verified" in final, final


@pytest.mark.asyncio
async def test_real_turn_cap_keeps_the_hourglass_and_wording(hermetic_terminal):  # noqa: F811
    snap, final = await _run_worker(
        hermetic_terminal, "turn_cap", CLAUDE_TURN_CAP, "claude-stream-json"
    )
    assert snap["state"] == "incomplete", snap
    head = final.splitlines()[0]
    assert head.startswith(outcome_mark("unfinished", "☘️")), final
    assert not head.startswith("✅"), final
    assert not head.startswith(outcome_mark("done", "✅")), final
    assert "not verified" not in final, final
    assert "ran out of steps before finishing" in final, final

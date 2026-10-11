"""The router's first call must not pay the one-time imports.

The warm-up is exercised in a fresh interpreter (the test process has the
modules loaded already, and the suite stubs the starter out): a slow module
stands in for the SDK imports, the router is routed with a fake auxiliary
client, and the contract is behavioural --

* ``start`` returns at once (construction is never blocked by the imports);
* the first ``plan_for_turn`` after it gets its verdict inside the default
  timeout;
* it is imports only: no auxiliary model call is made by the warm-up.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_SCRIPT = textwrap.dedent(
    """
    import json, os, sys, time, threading
    from types import SimpleNamespace

    sys.path.insert(0, {repo!r})
    sys.path.insert(0, {slowdir!r})

    from agent import router_prewarm, delegation_router as dr
    from agent import foreground_time_limit as fl

    # A slow SDK stand-in: importing it takes longer than any sane timeout
    # would leave for the first call.
    router_prewarm._MODULES = ("slow_sdk_standin",)

    calls = []
    import agent.auxiliary_client as ac

    def fake_call_llm(**kw):
        calls.append(kw.get("task"))
        # Mirrors the real first call: it needs the warm modules.
        import slow_sdk_standin  # noqa: F401
        body = json.dumps({{"mode": "single", "reason": "a real job", "tasks": [
            {{"goal": "audit the repo", "depends_on": [], "acceptance": "a written report"}}]}})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])

    ac.call_llm = fake_call_llm
    fl._detached_delivery_available = lambda: True

    agent = SimpleNamespace(
        valid_tool_names={{"delegate_task", "terminal"}}, platform="telegram",
        _delegate_depth=0, _subagent_id=None, session_id="s",
    )

    t0 = time.monotonic()
    started = router_prewarm.start(agent)
    start_seconds = time.monotonic() - t0
    again = router_prewarm.start(agent)
    time.sleep(1.2)  # the rest of agent construction / the user typing

    t1 = time.monotonic()
    note = dr.plan_for_turn(agent, "audit the whole repo and report", [])
    first_route = time.monotonic() - t1

    print(json.dumps({{
        "started": started, "started_again": again,
        "start_seconds": start_seconds, "first_route_seconds": first_route,
        "default_timeout": dr.load_settings().timeout_seconds,
        "model_calls": calls, "got_plan": bool(note),
        "slow_loaded": "slow_sdk_standin" in sys.modules,
    }}))
    """
)


def test_prewarm_never_blocks_and_the_first_route_fits_the_default_timeout(tmp_path):
    slowdir = tmp_path / "slow"
    slowdir.mkdir()
    (slowdir / "slow_sdk_standin.py").write_text("import time\ntime.sleep(1.5)\n")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path),
        "CLOVER_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.pop("CLOVER_IGNORE_USER_CONFIG", None)
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT.format(repo=str(REPO), slowdir=str(slowdir))],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["started"] is True
    assert out["started_again"] is False  # once per process
    assert out["start_seconds"] < 0.5, out  # construction is never blocked
    assert out["slow_loaded"] is True
    # The 1.5 s import overlapped the 1.2 s before the first turn: the route
    # only waited for what was left of it, and fit the timeout.
    assert out["first_route_seconds"] < 1.0, out
    assert out["first_route_seconds"] < out["default_timeout"], out
    assert out["got_plan"] is True, out  # a verdict, not a missed deadline
    assert out["model_calls"] == ["delegation_router"], out  # one call: the route

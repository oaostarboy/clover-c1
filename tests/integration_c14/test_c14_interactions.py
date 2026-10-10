"""C1.4 cross-patch interaction checks (P1 foreground limit, P2 start folder, P3 boot identity).

Each patch has its own suite; these tests pin the places where two of them meet.
Real AIAgent / delegate_task / async registry / git / terminal tool / start_gateway,
a temp CLOVER_HOME, a fake monotonic clock. Only the provider client is faked.
"""
from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from agent import foreground_time_limit as fl
from tools import approval, async_delegation as ad, delegate_tool as dt
from tools import terminal_tool as tt


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
    return path


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ── P2 + P1: fresh gateway chat, required isolation, foreground limit on ──

@pytest.fixture
def fresh_chat(tmp_path, monkeypatch):
    """A telegram chat that has never run a terminal command, in a gateway whose
    start folder is a real git repo, with required isolation and the limit on."""
    from run_agent import AIAgent
    from gateway.session_context import clear_session_vars, set_session_vars

    home = tmp_path / "home"
    (home / ".clover").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLOVER_HOME", str(home / ".clover"))
    from clover_cli.config_defaults import DEFAULT_CONFIG

    (home / ".clover" / "config.yaml").write_text(yaml.safe_dump({
        "_config_version": DEFAULT_CONFIG["_config_version"],
        "terminal": {"backend": "local", "local_persistent": False},
        "delegation": {
            "worktree_isolation": True,
            "worktree_isolation_required": True,
            "foreground_time_limit_seconds": 300,
            "max_iterations": 3,
        },
    }))
    project = _repo(tmp_path / "gateway-start-folder")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    monkeypatch.setattr(tt, "_gateway_start_cwd", None)
    assert tt.capture_gateway_start_cwd(str(project)) is True

    clock = _FakeClock()
    monkeypatch.setattr(fl, "_now", clock)
    ad._reset_for_tests()
    monkeypatch.setattr("run_agent.OpenAI", MagicMock())
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: {
        "model": "test/model", "provider": None, "base_url": None,
        "api_key": None, "api_mode": None, "command": None, "args": None,
    })

    parent = AIAgent(model="test/model", api_key="test-key-1234567890",
                     base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                     skip_context_files=True, skip_memory=True,
                     skip_background_review=True,
                     enabled_toolsets=["terminal", "delegation"], platform="telegram")
    task = parent.session_id
    key = "agent:main:telegram:dm:fresh-chat"

    caller = threading.current_thread()
    ran_on: list = []
    isolated_cwd: list = []
    selected: list = []
    import tools.subagent_worktree as sw

    real_prepare = sw.prepare_subagent_worktree

    def observe_prepare(cwd, *a, **kw):
        selected.append(cwd)
        return real_prepare(cwd, *a, **kw)

    monkeypatch.setattr(sw, "prepare_subagent_worktree", observe_prepare)
    children: list = []
    build = dt._build_child_agent

    def build_child(**kw):
        child = build(**kw)
        child._cached_system_prompt = "Complete the isolated test."
        child._use_prompt_caching = False
        child.compression_enabled = False
        child.save_trajectories = False
        child._fallback_chain = []
        client = MagicMock()

        def respond(**kwargs):
            ran_on.append(threading.current_thread())
            isolated_cwd.append(tt.get_session_cwd(child._subagent_id))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="test completed", tool_calls=None),
                finish_reason="stop")], model="test/model", usage=None)

        client.chat.completions.create.side_effect = respond
        child.client = client
        children.append(child)
        return child

    monkeypatch.setattr(dt, "_build_child_agent", build_child)

    executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(ad, "_get_executor", lambda n: executor)
    done = threading.Event()
    finished: dict = {}
    finalize = ad._finalize_batch

    def finish(ident, combined, status):
        try:
            finished[ident] = combined
            return finalize(ident, combined, status)
        finally:
            done.set()

    monkeypatch.setattr(ad, "_finalize_batch", finish)

    token = approval.set_current_session_key(key)
    context_token = set_session_vars(session_key=key, session_id=task,
                                     platform="telegram", source="telegram")
    fl.begin_turn(parent)
    state = SimpleNamespace(parent=parent, task=task, key=key, project=project,
                            caller=caller, ran_on=ran_on, isolated_cwd=isolated_cwd,
                            selected=selected, done=done, finished=finished,
                            children=children)
    try:
        yield state
    finally:
        executor.shutdown(wait=True)
        clear_session_vars(context_token)
        approval.reset_current_session_key(token)
        for k in [key, task, "default"] + [c._subagent_id for c in children]:
            tt.clear_task_env_overrides(k)
        tt.cleanup_all_environments()
        ad._reset_for_tests()


def test_fresh_chat_under_limit_and_required_isolation_dispatches_a_background_helper(fresh_chat):
    s = fresh_chat
    # The three preconditions that make this the P2 + P1 corner.
    assert tt.get_session_cwd(s.key) is None and tt.get_session_cwd(s.task) is None
    assert dt._get_worktree_isolation_required() is True
    assert fl.remaining_seconds(s.parent) is not None, "foreground limit must be active"

    raw = s.parent._invoke_tool("delegate_task", {"goal": "complete isolated test"}, s.task)
    out = json.loads(raw)

    # P1: handed to a background helper, not run inline.
    assert out.get("status") == "dispatched", out
    assert out.get("mode") == "background", out
    assert s.done.wait(25), "background helper never finished"
    results = s.finished[out["delegation_id"]]["results"]

    # P2: the folder resolved from the gateway start folder, so required
    # isolation could place the helper instead of blocking it.
    assert s.selected == [str(s.project)], (s.selected, results)
    assert results[0]["status"] == "completed", results
    assert results[0]["workspace_isolation"]["status"] == "isolated", results
    # Not inline: the child's model call ran off the caller's thread, in a
    # worktree that is not the project folder itself.
    assert s.ran_on and all(t is not s.caller for t in s.ran_on)
    assert s.isolated_cwd and s.isolated_cwd[0] != str(s.project)
    # The chat itself still has no cwd record (snapshot is child-only).
    assert tt.get_session_cwd(s.key) is None


def test_without_the_start_folder_snapshot_the_same_call_is_blocked(fresh_chat, monkeypatch):
    """Control: take P2's snapshot away and the corner is blocked again, so the
    test above passes because of P2 and not because isolation was never asked."""
    s = fresh_chat
    monkeypatch.setattr(tt, "_gateway_start_cwd", None)

    out = json.loads(s.parent._invoke_tool(
        "delegate_task", {"goal": "complete isolated test"}, s.task))

    # Still background (P1)...
    assert out.get("status") == "dispatched", out
    assert s.done.wait(25), "background helper never finished"
    results = s.finished[out["delegation_id"]]["results"]
    # ...but isolation had no folder to work from: blocked, never isolated.
    assert results[0]["status"] == "error", results
    assert "Required workspace isolation was not available" in results[0]["error"], results
    assert (results[0].get("workspace_isolation") or {}).get("status") != "isolated", results
    assert all(folder is None for folder in s.selected), s.selected
    assert not s.ran_on, "a blocked helper must not have called the model"


# ── P2 + P3: start_gateway takes both snapshots, once, before cron ────────

class _RecordingCron:
    """In-process-shaped provider that records when the scheduler is started."""

    def __new__(cls, events):
        from cron.scheduler_provider import InProcessCronScheduler

        class Recorder(InProcessCronScheduler):
            def start(self, stop_event, **kw):
                events.append("cron.start")
                stop_event.wait(5)

            def stop(self):
                events.append("cron.stop")

        return Recorder()


@pytest.mark.asyncio
async def test_start_gateway_takes_start_cwd_and_boot_identity_once_before_cron(tmp_path, monkeypatch):
    import gateway.run as gr
    from clover_cli import build_info
    from gateway.config import GatewayConfig

    home = tmp_path / "clover-home"
    home.mkdir()
    project = tmp_path / "start-folder"
    project.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(project))
    monkeypatch.setenv("CLOVER_EXEC_ASK", "")  # start_gateway sets it; restore after
    monkeypatch.setattr(gr, "_GATEWAY_START_CWD", str(project))
    monkeypatch.setattr(tt, "_gateway_start_cwd", None)
    monkeypatch.setattr(build_info, "_boot_identity", None)
    assert tt.get_gateway_start_cwd() is None and build_info.get_boot_identity() is None

    events: list[str] = []
    real_cwd, real_identity = tt.capture_gateway_start_cwd, build_info.capture_boot_identity

    def cwd_spy(cwd):
        events.append("start_cwd")
        return real_cwd(cwd)

    def identity_spy():
        events.append("boot_identity")
        return real_identity()

    monkeypatch.setattr(tt, "capture_gateway_start_cwd", cwd_spy)
    monkeypatch.setattr(build_info, "capture_boot_identity", identity_spy)

    class _Runner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self.should_exit_cleanly = False
            self.should_exit_with_failure = False
            self.exit_reason = None
            self.exit_code = None
            self._running = True
            self._draining = False
            self._external_drain_active = False
            self._restart_requested = False
            self._restart_via_service = False
            self._platform_lock_takeover_on_start = False

        async def start(self):
            events.append("runner.start")
            return True

        async def wait_for_shutdown(self):
            events.append("wait_for_shutdown")

        async def stop(self):
            return None

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("clover_logging.setup_logging", lambda clover_home, mode: tmp_path)
    monkeypatch.setattr("clover_logging._add_rotating_handler", lambda *a, **k: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", _Runner)
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler",
                        lambda *a, **k: _RecordingCron(events))

    await gr.start_gateway(config=GatewayConfig(), replace=False, verbosity=None)

    assert events.count("start_cwd") == 1, events
    assert events.count("boot_identity") == 1, events
    assert events.count("cron.start") == 1, events
    assert events.index("start_cwd") < events.index("cron.start"), events
    assert events.index("boot_identity") < events.index("cron.start"), events
    # Both snapshots really landed, and a later mutation (cron rewrites
    # TERMINAL_CWD) does not move the frozen start folder.
    monkeypatch.setenv("TERMINAL_CWD", "/mutated/by/cron")
    assert tt.get_gateway_start_cwd() == str(project)
    assert build_info.get_boot_identity() is not None

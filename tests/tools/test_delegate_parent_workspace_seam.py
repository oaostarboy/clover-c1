"""Real AIAgent/terminal dispatcher -> detached delegate workspace admission.

Only provider/client edges are mocked. Git, terminal cwd markers, SQLite,
child construction, async admission and worktree preparation are real.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools import approval, async_delegation as ad, delegate_tool as dt
from tools import terminal_tool as tt


def repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
    return path


@pytest.fixture
def seam(tmp_path, monkeypatch):
    from run_agent import AIAgent
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLOVER_HOME", str(home / ".clover"))
    Path(home / ".clover").mkdir()
    config = Path(home / ".clover/config.yaml")
    config.write_text("terminal:\n  backend: local\n  local_persistent: false\n"
                      "delegation:\n  worktree_isolation: true\n"
                      "  worktree_isolation_required: true\n  max_iterations: 3\n")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    # Provider-only edge. Nothing in the workspace/session path is mocked.
    monkeypatch.setattr("run_agent.OpenAI", MagicMock())
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: {
        "model": "test/model", "provider": None, "base_url": None,
        "api_key": None, "api_mode": None, "command": None, "args": None,
    })
    parent = AIAgent(model="test/model", api_key="test-key-1234567890",
                     base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                     skip_context_files=True, skip_memory=True,
                     skip_background_review=True, enabled_toolsets=["terminal", "delegation"],
                     platform="telegram")
    assert not hasattr(parent, "_current_task_id")
    # This is the gateway's trusted raw session id, distinct from its chat key.
    task = parent.session_id
    key = "agent:main:telegram:dm:workspace-repair"
    children, calls, selected = [], [], []
    build = dt._build_child_agent
    prepare = __import__("tools.subagent_worktree", fromlist=["prepare_subagent_worktree"])
    real_prepare = prepare.prepare_subagent_worktree

    def observe_prepare(cwd, *a, **kw):
        selected.append(cwd)
        return real_prepare(cwd, *a, **kw)

    monkeypatch.setattr(prepare, "prepare_subagent_worktree", observe_prepare)

    def build_child(**kw):
        child = build(**kw)
        child._cached_system_prompt = "Complete the isolated test."
        child._use_prompt_caching = False
        child.compression_enabled = False
        child.save_trajectories = False
        child._fallback_chain = []
        client = MagicMock()
        def respond(**kwargs):
            calls.append(tt.get_session_cwd(child._subagent_id))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="test completed", tool_calls=None),
                finish_reason="stop")], model="test/model", usage=None)
        client.chat.completions.create.side_effect = respond
        child.client = client
        children.append(child)
        return child
    monkeypatch.setattr(dt, "_build_child_agent", build_child)
    # Own finite executor, real async dispatch; no inherited approval context.
    executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(ad, "_get_executor", lambda n: executor)
    monkeypatch.setattr(ad, "_records", {})
    done = threading.Event()
    finished = {}
    finalize = ad._finalize_batch
    def finish(ident, combined, status):
        try:
            finished[ident] = combined
            return finalize(ident, combined, status)
        finally:
            done.set()
    monkeypatch.setattr(ad, "_finalize_batch", finish)
    token = approval.set_current_session_key(key)
    # Real gateway session_context route used by background-delivery admission.
    from gateway.session_context import set_session_vars, clear_session_vars
    context_token = set_session_vars(session_key=key, session_id=task,
                                     platform="telegram", source="telegram")
    state = SimpleNamespace(parent=parent, task=task, key=key, tmp=tmp_path,
                            config=config, children=children, calls=calls,
                            selected=selected, done=done, finished=finished,
                            executor=executor)
    try:
        yield state
    finally:
        executor.shutdown(wait=True)
        clear_session_vars(context_token)
        approval.reset_current_session_key(token)
        for k in [key, task, "default"] + [c._subagent_id for c in children]:
            tt.clear_task_env_overrides(k)
        # Only the terminal backend created by this test process.
        tt.cleanup_all_environments()


def cd(s, path, workdir=None):
    args = {"command": "cd " + shlex.quote(str(path)) + " && pwd", "timeout": 10}
    if workdir:
        args["workdir"] = str(workdir)
    output = json.loads(s.parent._invoke_tool("terminal", args, s.task))
    assert output.get("exit_code") == 0, output
    assert str(path) in output["output"]
    return output


def dispatch(s, tasks=None):
    output = json.loads(s.parent._invoke_tool("delegate_task",
        {"tasks": tasks} if tasks else {"goal": "complete isolated test"}, s.task))
    assert output.get("status") == "dispatched", output
    assert s.done.wait(25), "async batch did not finish"
    return s.finished[output["delegation_id"]]["results"]


def test_gateway_observed_terminal_cd_reaches_detached_worktree_admission(seam):
    s = seam
    project = repo(s.tmp / "project")
    cd(s, project)  # no per-command workdir: actual observed-cwd marker
    assert tt.get_session_cwd(s.key) == str(project)
    assert tt.get_session_cwd(s.task) is None
    results = dispatch(s)
    assert s.selected == [str(project)], results
    assert results[0]["workspace_isolation"]["status"] == "isolated", results
    assert len(s.calls) == 1
    assert s.calls[0] != str(project)
    assert tt.get_session_cwd(s.key) == str(project)

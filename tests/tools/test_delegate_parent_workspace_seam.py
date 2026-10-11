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
            start_cwd = tt.get_session_cwd(child._subagent_id)
            calls.append(start_cwd)
            observed = json.loads(child._invoke_tool("terminal", {
                "command": "pwd", "timeout": 10}, child._subagent_id))
            assert observed["exit_code"] == 0, observed
            if start_cwd:
                assert start_cwd in observed["output"], observed
            if getattr(state, "child_cd", None):
                # Sync/nested execution can retain the root approval context.
                # That authority must not select the child's cwd record.
                inherited = approval.set_current_session_key(state.key)
                try:
                    result = json.loads(child._invoke_tool("terminal", {
                        "command": "cd " + shlex.quote(str(state.child_cd)) + " && pwd",
                        "timeout": 10}, child._subagent_id))
                    assert result["exit_code"] == 0, result
                finally:
                    approval.reset_current_session_key(inherited)
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


def test_child_cd_does_not_overwrite_parent_with_inherited_approval(seam):
    s = seam
    project = repo(s.tmp / "project")
    s.child_cd = s.tmp / "child-directory"
    s.child_cd.mkdir()
    cd(s, project)
    results = dispatch(s)
    assert results[0]["workspace_isolation"]["status"] == "isolated", results
    assert tt.get_session_cwd(s.key) == str(project)
    assert tt.get_session_cwd(s.children[0]._subagent_id) == str(s.child_cd)


def test_transient_workdir_does_not_redirect_delegate(seam):
    s = seam
    original, transient = repo(s.tmp / "original"), repo(s.tmp / "transient")
    cd(s, original)
    cd(s, transient, workdir=transient)
    assert tt.get_session_cwd(s.key) == str(original)
    results = dispatch(s)
    assert s.selected == [str(original)], results


def test_queued_job_keeps_original_workspace_after_parent_new_question(seam):
    s = seam
    original, later = repo(s.tmp / "original"), repo(s.tmp / "later")
    cd(s, original)
    queued, release = threading.Event(), threading.Event()
    submit = s.executor.submit
    def delayed(fn, *a, **kw):
        def run():
            queued.set()
            assert release.wait(10)
            return fn(*a, **kw)
        return submit(run)
    s.executor.submit = delayed
    try:
        output = json.loads(s.parent._invoke_tool("delegate_task", {"goal": "complete isolated test"}, s.task))
        assert output["status"] == "dispatched", output
        assert queued.wait(5)
        cd(s, later)
        s.parent.terminal_cwd = s.parent.cwd = str(later)
        release.set()
        assert s.done.wait(25)
        results = s.finished[output["delegation_id"]]["results"]
        assert s.selected == [str(original)], results
        assert results[0]["workspace_isolation"]["status"] == "isolated", results
        assert tt.get_session_cwd(s.key) == str(later)
    finally:
        release.set()


@pytest.mark.parametrize("required", [True, False])
def test_missing_binding_never_uses_foreign_default_or_global_git(seam, monkeypatch, required):
    s = seam
    foreign = repo(s.tmp / "foreign")
    tt.record_session_cwd("default", str(foreign))
    tt.record_session_cwd("foreign-profile:foreign-route", str(foreign))
    monkeypatch.setenv("TERMINAL_CWD", str(foreign))
    s.parent.cwd = str(foreign)
    if not required:
        s.config.write_text(s.config.read_text().replace("worktree_isolation_required: true", "worktree_isolation_required: false"))
    results = dispatch(s)
    assert s.selected == [None], results
    assert results[0]["workspace_isolation"]["status"] == ("blocked" if required else "shared")
    assert bool(s.calls) is (not required)


def test_cli_without_approval_key_uses_runtime_task_cwd(seam):
    s = seam
    from gateway.session_context import set_session_vars
    s.parent.platform = "cli"
    approval.set_current_session_key("")
    set_session_vars(platform="cli", source="cli", session_id=s.task)
    project = repo(s.tmp / "cli-project")
    cd(s, project)
    assert tt.get_session_cwd(s.task) == str(project)
    results = dispatch(s)
    assert s.selected == [str(project)], results
    assert results[0]["workspace_isolation"]["status"] == "isolated"


def test_two_actual_workers_have_distinct_branches(seam):
    s = seam
    project = repo(s.tmp / "project")
    cd(s, project)
    results = dispatch(s, tasks=[{"goal": "worker one"}, {"goal": "worker two"}])
    assert len(results) == 2
    assert all(r["workspace_isolation"]["status"] == "isolated" for r in results), results
    assert len({r["workspace_isolation"]["branch"] for r in results}) == 2
    assert len(set(s.calls)) == 2
    assert tt.get_session_cwd(s.key) == str(project)


@pytest.mark.parametrize("foreign_key", ["profile-b:telegram:dm:other", "profile-a:telegram:group:other"])
def test_distinct_profile_and_route_record_never_redirects_original(seam, foreign_key):
    s = seam
    from gateway.session_context import set_session_vars
    original, foreign = repo(s.tmp / "original"), repo(s.tmp / "foreign")
    cd(s, original)
    token = approval.set_current_session_key(foreign_key)
    try:
        set_session_vars(platform="telegram", source="telegram", profile="profile-b", session_key=foreign_key, session_id="foreign-raw-task")
        cd(s, foreign)
    finally:
        approval.reset_current_session_key(token)
        set_session_vars(platform="telegram", source="telegram", session_key=s.key, session_id=s.task)
    assert tt.get_session_cwd(foreign_key) == str(foreign)
    results = dispatch(s)
    assert s.selected == [str(original)], results


def test_model_workspace_authority_fields_are_not_forwarded(seam):
    s = seam
    project, foreign = repo(s.tmp / "project"), repo(s.tmp / "foreign")
    cd(s, project)
    output = json.loads(s.parent._invoke_tool("delegate_task", {
        "goal": "complete isolated test", "parent_task_id": "foreign-task",
        "parent_workspace": ["foreign-task", str(foreign)], "session_key": "foreign", "cwd": str(foreign)}, s.task))
    assert output["status"] == "dispatched", output
    assert s.done.wait(25)
    assert s.selected == [str(project)]


# ── Fresh gateway chat: no recorded cwd yet (C1.4 P2) ────────────────────────
#
# A bound gateway session that has not run a terminal command has no cwd
# record. Its first command would start in the folder the gateway captured at
# startup, so a helper must start there too instead of being blocked.


def _start_folder(monkeypatch, path):
    """Install the gateway's startup snapshot the way start_gateway() does."""
    monkeypatch.setattr(tt, "_gateway_start_cwd", str(path) if path else None, raising=False)


def _blank(s, name="chat-home"):
    path = s.tmp / name
    path.mkdir()
    return path


def _set_backend(s, backend):
    s.config.write_text(s.config.read_text().replace("backend: local", f"backend: {backend}"))


def test_fresh_gateway_chat_helper_starts_in_resolved_non_git_dir(seam, monkeypatch):
    s = seam
    chat_home = _blank(s)
    _start_folder(monkeypatch, chat_home)
    assert tt.get_session_cwd(s.key) is None
    results = dispatch(s)
    assert s.selected == [str(chat_home)], results
    iso = results[0]["workspace_isolation"]
    assert iso["status"] == "shared" and iso.get("no_checkout") is True, results
    # The child really ran, and started in the resolved folder.
    assert s.calls == [str(chat_home)]
    # Snapshot lands on the CHILD only: the chat itself still has no record.
    assert tt.get_session_cwd(s.key) is None
    assert tt.get_session_cwd(s.task) is None
    assert tt.get_session_cwd("default") is None


def test_fresh_gateway_chat_resolved_git_repo_gets_a_real_worktree(seam, monkeypatch):
    s = seam
    project = repo(s.tmp / "project")
    _start_folder(monkeypatch, project)
    results = dispatch(s)
    assert s.selected == [str(project)], results
    iso = results[0]["workspace_isolation"]
    assert iso["status"] == "isolated", results
    worktree = Path(iso["path"])
    assert worktree.parent.resolve() == (project / ".worktrees").resolve()
    assert iso["branch"].startswith("clover-subagent/")
    # The child's own `pwd` ran inside that worktree (clean ones are pruned
    # afterwards, so existence on disk is not the proof).
    assert s.calls == [str(worktree)]
    assert tt.get_session_cwd(s.key) is None


def test_fresh_gateway_chat_symlink_to_repo_gets_a_real_worktree(seam, monkeypatch):
    s = seam
    project = repo(s.tmp / "project")
    link = s.tmp / "chat-link"
    link.symlink_to(project, target_is_directory=True)
    _start_folder(monkeypatch, link)
    results = dispatch(s)
    iso = results[0]["workspace_isolation"]
    assert iso["status"] == "isolated", results
    assert Path(iso["path"]).parent.resolve() == (project / ".worktrees").resolve()
    assert tt.get_session_cwd(s.key) is None


def test_fresh_gateway_chat_symlink_to_non_git_dir_still_runs(seam, monkeypatch):
    s = seam
    target = _blank(s, "plain")
    link = s.tmp / "plain-link"
    link.symlink_to(target, target_is_directory=True)
    _start_folder(monkeypatch, link)
    results = dispatch(s)
    assert results[0]["workspace_isolation"]["status"] == "shared", results
    assert len(s.calls) == 1


def test_fresh_gateway_chat_unavailable_start_folder_stays_blocked(seam, monkeypatch):
    s = seam
    # A real repo sits at home and in the process env: neither may be borrowed.
    home_repo = repo(Path.home() / "home-repo")
    monkeypatch.setenv("TERMINAL_CWD", str(home_repo))
    _start_folder(monkeypatch, s.tmp / "deleted-since-startup")
    results = dispatch(s)
    assert s.selected == [None], results
    assert results[0]["workspace_isolation"]["status"] == "blocked"
    assert s.calls == []
    assert tt.get_session_cwd(s.key) is None


def test_fresh_gateway_chat_without_snapshot_stays_blocked(seam, monkeypatch):
    s = seam
    _start_folder(monkeypatch, None)
    results = dispatch(s)
    assert s.selected == [None], results
    assert results[0]["workspace_isolation"]["status"] == "blocked"
    assert s.calls == []


def test_cron_mutated_terminal_cwd_does_not_move_the_start_folder(seam, monkeypatch):
    s = seam
    chat_home = _blank(s)
    cron_job_dir = repo(s.tmp / "cron-workdir")
    _start_folder(monkeypatch, chat_home)
    # A workdir cron job overwrites the process-global env for its whole run.
    monkeypatch.setenv("TERMINAL_CWD", str(cron_job_dir))
    results = dispatch(s)
    assert s.selected == [str(chat_home)], results
    assert results[0]["workspace_isolation"]["status"] == "shared"
    assert not (cron_job_dir / ".worktrees").exists()


def test_non_local_backend_still_refuses_with_a_start_folder(seam, monkeypatch):
    s = seam
    project = repo(s.tmp / "project")
    _start_folder(monkeypatch, project)
    _set_backend(s, "docker")
    results = dispatch(s)
    assert results[0]["workspace_isolation"]["status"] == "blocked", results
    assert "backend" in results[0]["workspace_isolation"]["reason"]
    assert s.calls == []
    assert not (project / ".worktrees").exists()


def test_interleaved_sessions_each_keep_their_own_start_folder(seam, monkeypatch):
    s = seam
    from gateway.session_context import set_session_vars
    chat_home = _blank(s)
    other_project = repo(s.tmp / "other-project")
    other_key = "agent:main:telegram:dm:other-chat"
    _start_folder(monkeypatch, chat_home)

    def capture_as(key):
        token = approval.set_current_session_key(key)
        try:
            set_session_vars(platform="telegram", source="telegram",
                             session_key=key, session_id=s.task)
            return dt._capture_parent_workspace(s.parent, s.task)[1]
        finally:
            approval.reset_current_session_key(token)
            set_session_vars(platform="telegram", source="telegram",
                             session_key=s.key, session_id=s.task)

    # Chat B has run a command and moved; chat A is still fresh.
    tt.record_session_cwd(other_key, str(other_project))
    try:
        assert capture_as(s.key) == str(chat_home)
        assert capture_as(other_key) == str(other_project)
        assert capture_as(s.key) == str(chat_home)
        # Neither capture wrote a record for the other.
        assert tt.get_session_cwd(s.key) is None
        assert tt.get_session_cwd(other_key) == str(other_project)
    finally:
        tt.clear_session_cwd(other_key)
    # And a real dispatch from the still-fresh chat uses its own folder.
    results = dispatch(s)
    assert s.selected == [str(chat_home)], results


def test_recorded_chat_keeps_its_own_cwd_over_the_start_folder(seam, monkeypatch):
    s = seam
    chat_home, project = _blank(s), repo(s.tmp / "project")
    _start_folder(monkeypatch, chat_home)
    cd(s, project)
    results = dispatch(s)
    assert s.selected == [str(project)], results
    assert results[0]["workspace_isolation"]["status"] == "isolated"


def test_start_folder_is_not_used_below_depth_zero(seam, monkeypatch):
    s = seam
    _start_folder(monkeypatch, _blank(s))
    s.parent._delegate_depth = 1
    assert dt._capture_parent_workspace(s.parent, "orchestrator-child-task")[1] is None


def test_start_folder_is_not_used_for_the_cli(seam, monkeypatch):
    s = seam
    from gateway.session_context import set_session_vars
    start = _blank(s)
    _start_folder(monkeypatch, start)
    s.parent.platform = "cli"
    approval.set_current_session_key("")
    set_session_vars(platform="cli", source="cli", session_id=s.task)
    # The CLI keeps its own startup-directory hint; the gateway snapshot is
    # never consulted for it.
    assert dt._capture_parent_workspace(s.parent, s.task)[1] != str(start)


@pytest.mark.parametrize("with_override", [False, True])
def test_terminal_first_command_runs_where_the_helper_resolver_says(seam, monkeypatch, with_override):
    """Parity: the delegate fallback and the terminal share one resolver."""
    s = seam
    start = _blank(s, "start-folder")
    monkeypatch.setenv("TERMINAL_CWD", str(start))
    _start_folder(monkeypatch, start)
    expected = str(start)
    if with_override:
        overridden = _blank(s, "workspace-override")
        tt.register_task_env_overrides(s.task, {"cwd": str(overridden)})
        expected = str(overridden)
    resolved = tt.resolve_first_command_cwd(
        s.task, s.key, default_cwd=str(start), env_type="local")
    assert resolved == expected
    out = json.loads(s.parent._invoke_tool(
        "terminal", {"command": "pwd", "timeout": 10}, s.task))
    assert out["exit_code"] == 0, out
    assert out["output"].strip() == resolved


def test_capture_gateway_start_cwd_is_write_once_and_absolute_only(monkeypatch):
    monkeypatch.setattr(tt, "_gateway_start_cwd", None, raising=False)
    assert tt.get_gateway_start_cwd() is None
    assert tt.capture_gateway_start_cwd("") is False
    assert tt.capture_gateway_start_cwd("relative/dir") is False
    assert tt.capture_gateway_start_cwd(None) is False
    assert tt.get_gateway_start_cwd() is None
    assert tt.capture_gateway_start_cwd("/first/folder") is True
    assert tt.capture_gateway_start_cwd("/second/folder") is False
    assert tt.get_gateway_start_cwd() == "/first/folder"

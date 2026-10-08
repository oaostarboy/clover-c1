"""Behavior contract for scripts/ci/release_publish.py (the publish half of Release cut).

A scripted fake ``gh``/``git`` runner stands in for GitHub so the safety rules are
proven without publishing anything: no tag unless the required check was green on
the exact pushed SHA, no merge when the head moved, no tag when the merge commit
is not (base, tested head), and a dry run issues no write command at all.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "release_publish.py"
spec = importlib.util.spec_from_file_location("release_publish", SCRIPT)
assert spec is not None and spec.loader is not None
rp = importlib.util.module_from_spec(spec)
sys.modules["release_publish"] = rp
spec.loader.exec_module(rp)

BASE = "b" * 40
HEAD = "a" * 40
MERGE = "c" * 40
AGG = rp.REQUIRED_CHECK


def run(name, status="completed", conclusion: str = "success"):
    return {"name": name, "status": status, "conclusion": conclusion}


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_green_needs_the_aggregate_check_to_succeed():
    gate = rp.evaluate_checks([run("Lint"), run(AGG)], [])
    assert gate.state == "green"


def test_no_aggregate_check_yet_is_absent_not_green():
    assert rp.evaluate_checks([run("Detect affected areas")], []).state == "absent"
    assert rp.evaluate_checks([], []).state == "absent"


def test_aggregate_still_running_is_pending():
    assert rp.evaluate_checks([run(AGG, "in_progress", "")], []).state == "pending"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_aggregate_failing_is_red(conclusion):
    assert rp.evaluate_checks([run(AGG, conclusion=conclusion)], []).state == "red"


def test_any_other_failing_check_is_red_even_when_the_aggregate_passed():
    gate = rp.evaluate_checks([run(AGG), run("Docker build", conclusion="failure")], [])
    assert gate.state == "red" and "Docker build" in gate.detail


def test_failing_commit_status_is_red():
    assert rp.evaluate_checks([run(AGG)], [{"context": "legacy/ci", "state": "failure"}]).state == "red"


def test_skipped_checks_and_slow_advisory_jobs_do_not_block():
    gate = rp.evaluate_checks([run(AGG), run("JS", conclusion="skipped"), run("image build", "in_progress", "")], [])
    assert gate.state == "green" and "image build" in gate.detail


# ---------------------------------------------------------------------------
# the flow, with a scripted GitHub
# ---------------------------------------------------------------------------


class Fake:
    def __init__(self, *, gate_runs=None, head_after=HEAD, parents=(BASE, HEAD), behind=0, tag_exists=False,
                 release_exists=False, branch_exists=False, remote_main=BASE, version_in_merge=True):
        self.calls: list[tuple[str, ...]] = []
        self.gate_runs = [run(AGG)] if gate_runs is None else gate_runs
        self.head_after, self.parents, self.behind = head_after, list(parents), behind
        self.tag_exists, self.release_exists, self.branch_exists = tag_exists, release_exists, branch_exists
        self.remote_main, self.version_in_merge = remote_main, version_in_merge
        self.committed = self.merged = False
        self.head_reads = 0

    def __call__(self, cmd):
        cmd = tuple(cmd)
        self.calls.append(cmd)
        text = " ".join(cmd)
        out, code = "", 0
        if " commit " in f" {text} ":
            self.committed = True
        if cmd[:3] == ("gh", "pr", "merge"):
            self.merged = True
        if cmd[:2] == ("git", "rev-parse"):
            out = MERGE if "^{commit}" in text else (HEAD if self.committed else BASE)
        elif cmd[:2] == ("git", "ls-remote"):
            if "refs/heads/main" in text:
                out = f"{self.remote_main}\trefs/heads/main"
            elif "--heads" in text:
                out = "x\trefs/heads/release/c1.1.4" if self.branch_exists else ""
            else:
                out = "x\trefs/tags/v1.1.4" if self.tag_exists else ""
        elif cmd[:3] == ("gh", "release", "view"):
            if "-q" in cmd:
                out = "https://github.com/o/r/releases/tag/v1.1.4"
            else:
                code = 0 if self.release_exists else 1
        elif cmd[:3] == ("git", "diff", "--cached"):
            out = "\n".join(["clover_cli/__init__.py", "pyproject.toml", "uv.lock"])
        elif cmd[:3] == ("gh", "pr", "create"):
            out = "https://github.com/o/r/pull/99"
        elif cmd[:3] == ("gh", "pr", "view"):
            if "headRefOid" in text:
                self.head_reads += 1
                out = HEAD if self.head_reads == 1 else self.head_after  # 1st read = right after PR creation
            else:
                out = f"{'MERGED' if self.merged else 'OPEN'} {MERGE}"
        elif cmd[:2] == ("gh", "api"):
            path = cmd[2]
            if "check-runs" in path:
                out = "\n".join(json.dumps(r) for r in self.gate_runs)
            elif path.endswith("/status"):
                out = json.dumps({"statuses": []})
            elif "/compare/" in path:
                out = json.dumps({"behind_by": self.behind})
            elif f"/commits/{MERGE}" in path:
                out = json.dumps({"parents": [{"sha": p} for p in self.parents]})
        elif cmd[:2] == ("git", "show"):
            out = '__version__ = "1.1.4"' if self.version_in_merge else '__version__ = "1.1.3"'
        return subprocess.CompletedProcess(list(cmd), code, out + ("\n" if out else ""), "")

    def writes(self):
        w = []
        for c in self.calls:
            t = " ".join(c)
            if c[:2] in (("git", "push"), ("git", "switch"), ("git", "add")) or " commit " in f" {t} " or " tag -a " in f" {t} ":
                w.append(c)
            elif c[:3] in (("gh", "pr", "create"), ("gh", "pr", "merge"), ("gh", "release", "create")):
                w.append(c)
        return w


SUMMARY = {
    "version": "1.1.4", "name": "Clover C1.1.4", "release_date_string": "2026.10.8",
    "bullets": ["Fixed: a thing.", "New: another thing."],
    "changed_files": ["clover_cli/__init__.py", "pyproject.toml", "uv.lock"],
}


def ctx_for(fake, *, dry_run=False, token="elevated"):
    logs: list[str] = []
    ctx = rp.Ctx(repo="o/r", run=fake, dry_run=dry_run, log=logs.append, sleep=lambda s: None)
    ctx.logs = logs  # type: ignore[attr-defined]
    return ctx


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "elevated-token")
    monkeypatch.setenv("DEFAULT_GITHUB_TOKEN", "default-token")


def go(fake, tmp_path, **kw):
    ctx = ctx_for(fake, dry_run=kw.pop("dry_run", False))
    clock = iter(range(0, 10_000_000, 60))
    ctx.clock = lambda: next(clock)
    args = dict(base_branch="main", required=AGG, timeout_s=3600, poll_s=1, no_ci_timeout_s=600,
                github_token="default-token", probe_sha=None)
    args.update(kw)
    try:
        code = rp.cut(ctx, SUMMARY, tmp_path / "notes.md", **args)
    except rp.Abort as exc:
        return ctx, str(exc)
    return ctx, code


def test_happy_path_merges_with_match_head_commit_then_tags_the_merge_commit(tmp_path):
    fake = Fake()
    ctx, result = go(fake, tmp_path)
    assert result == 0
    flat = [" ".join(c) for c in fake.calls]
    merge = next(c for c in flat if c.startswith("gh pr merge"))
    assert f"--match-head-commit {HEAD}" in merge and "--merge" in merge
    tag = next(c for c in flat if " tag -a " in c)
    assert f"tag -a v1.1.4 {MERGE} -m Clover C1.1.4" in tag
    rel = next(c for c in flat if c.startswith("gh release create"))
    assert "v1.1.4" in rel and "--verify-tag" in rel and "--notes-file" in rel
    order = [flat.index(merge), flat.index(tag), flat.index(rel)]
    assert order == sorted(order)
    assert not any("--force" in c or " -f " in c for c in flat)
    assert any("pr create" in c and "--head release/c1.1.4" in c for c in flat)


def test_red_ci_stops_before_merge_tag_or_release(tmp_path):
    fake = Fake(gate_runs=[run(AGG, conclusion="failure")])
    _, result = go(fake, tmp_path)
    assert isinstance(result, str) and "CI is red" in result
    names = {" ".join(c[:3]) for c in fake.calls}
    assert "gh pr merge" not in names and "gh release create" not in names
    assert not any(" tag -a " in " ".join(c) for c in fake.calls)


def test_moved_head_while_waiting_stops_before_merge(tmp_path):
    fake = Fake(head_after="d" * 40)
    _, result = go(fake, tmp_path)
    assert isinstance(result, str) and "head moved" in result
    assert not any(c[:3] == ("gh", "pr", "merge") for c in fake.calls)


def test_ci_that_never_reports_explains_the_token_problem_and_leaves_the_pr_open(tmp_path):
    fake = Fake(gate_runs=[])
    _, result = go(fake, tmp_path, no_ci_timeout_s=120)
    assert isinstance(result, str) and "RELEASE_CUT_TOKEN" in result and "left open" in result
    assert not any(c[:3] == ("gh", "pr", "merge") for c in fake.calls)


def test_pending_ci_is_polled_until_it_turns_green(tmp_path):
    fake = Fake(gate_runs=[run(AGG, "in_progress", "")])
    polls = {"n": 0}
    inner = Fake.__call__

    def counting(self, cmd):
        if "check-runs" in " ".join(cmd):
            polls["n"] += 1
            if polls["n"] >= 3:
                self.gate_runs = [run(AGG)]
        return inner(self, cmd)

    Fake.__call__ = counting  # type: ignore[method-assign]
    try:
        _, result = go(fake, tmp_path)
    finally:
        Fake.__call__ = inner  # type: ignore[method-assign]
    assert result == 0 and polls["n"] >= 3


def test_main_having_moved_since_the_pr_was_tested_stops_before_merge(tmp_path):
    fake = Fake(behind=2)
    _, result = go(fake, tmp_path)
    assert isinstance(result, str) and "main moved by 2" in result
    assert not any(c[:3] == ("gh", "pr", "merge") for c in fake.calls)


def test_merge_commit_with_unexpected_parents_is_never_tagged(tmp_path):
    fake = Fake(parents=(BASE, "e" * 40))
    _, result = go(fake, tmp_path)
    assert isinstance(result, str) and "NOT tagging" in result and "RECOVERY" in result
    assert not any(" tag -a " in " ".join(c) for c in fake.calls)


def test_merge_without_the_new_version_is_never_tagged(tmp_path):
    fake = Fake(version_in_merge=False)
    _, result = go(fake, tmp_path)
    assert isinstance(result, str) and "does not contain" in result and "RECOVERY" in result
    assert not any(" tag -a " in " ".join(c) for c in fake.calls)


@pytest.mark.parametrize("kw,needle", [
    (dict(tag_exists=True), "tag v1.1.4 already exists"),
    (dict(release_exists=True), "release for v1.1.4 already exists"),
    (dict(branch_exists=True), "release/c1.1.4 already exists"),
    (dict(remote_main="f" * 40), "main moved"),
])
def test_preconditions_refuse_before_any_write(tmp_path, kw, needle):
    fake = Fake(**kw)
    _, result = go(fake, tmp_path)
    assert isinstance(result, str) and needle in result
    assert fake.writes() == []


def test_default_github_token_is_refused_because_it_cannot_trigger_ci(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "default-token")
    fake = Fake()
    _, result = go(fake, tmp_path)
    assert isinstance(result, str) and "default GITHUB_TOKEN" in result
    assert fake.writes() == []


def test_dry_run_issues_no_write_and_logs_every_would_step(tmp_path):
    fake = Fake(gate_runs=[run(AGG)])
    ctx, result = go(fake, tmp_path, dry_run=True, probe_sha=HEAD)
    assert result == 0
    assert fake.writes() == []
    text = "\n".join(ctx.logs)  # type: ignore[attr-defined]
    for needle in ("WOULD: git switch -c release/c1.1.4", "WOULD: git push origin release/c1.1.4", "WOULD: gh pr create",
                   "--match-head-commit", "WOULD: git tag -a v1.1.4", "WOULD: gh release create v1.1.4",
                   "gate probe", "green"):
        assert needle in text, needle


def test_dry_run_with_default_token_still_works_but_says_a_real_run_would_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "default-token")
    ctx, result = go(Fake(), tmp_path, dry_run=True)
    assert result == 0
    assert "real run would refuse" in "\n".join(ctx.logs)  # type: ignore[attr-defined]

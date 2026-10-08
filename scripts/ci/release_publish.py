#!/usr/bin/env python3
"""Publish half of the *Release cut* workflow (.github/workflows/release-cut.yml).

``scripts/release_cut.py`` has already validated RELEASE_NOTES.md and bumped the
four version spots in the working tree (or, with ``--dry-run``, shown that it
would).  This script turns that into the release, one gated step at a time::

    1. branch    release/cX.Y.Z from the exact main commit that was checked out
    2. commit    "release: <name> (vX.Y.Z)" -- the same four files as 253629ba
    3. PR        base main, body = the notes bullets
    4. wait      for the required check on THE SAME head SHA that was pushed
    5. merge     --merge --match-head-commit <that SHA>  (never a moved head)
    6. tag       annotated vX.Y.Z "<name>" on the merge commit (only if 4 was green
                 and the merge commit's parents are the base and the tested SHA)
    7. release   gh release create vX.Y.Z, body = the notes bullets

Nothing is forced.  An existing branch, tag or release stops the run.  If the
run stops after the merge the exact recovery commands are printed.

``--dry-run`` performs only read-only probes and prints every write it would
have done as ``WOULD: <command>``; it also evaluates the real check gate against
``--probe-sha`` (default: the checked-out commit) so the gate is exercised on
live data.  It needs no write permission.

Environment: ``GH_TOKEN`` (used by the ``gh`` CLI and by git over HTTPS).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

REQUIRED_CHECK = "All required checks pass"
GOOD = {"success", "skipped", "neutral"}
BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class Abort(Exception):
    """Stop the run; nothing further is attempted. ``message`` is user facing."""


# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(cwd: Path) -> Runner:
    def run(cmd: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    return run


@dataclass
class Ctx:
    repo: str
    run: Runner
    dry_run: bool
    log: Callable[[str], None] = print
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def out(self, *cmd: str, check: bool = True) -> str:
        proc = self.run(cmd)
        if check and proc.returncode != 0:
            raise Abort(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{(proc.stderr or proc.stdout).strip()}")
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def ok(self, *cmd: str) -> bool:
        return self.run(cmd).returncode == 0

    def api(self, path: str, *extra: str):
        return json.loads(self.out("gh", "api", path, *extra) or "null")

    def would(self, text: str) -> None:
        self.log(f"WOULD: {text}")

    def step(self, text: str) -> None:
        self.log(f"==> {text}")


# ---------------------------------------------------------------------------
# the check gate (pure; unit tested)
# ---------------------------------------------------------------------------


@dataclass
class Gate:
    state: str  # "green" | "red" | "pending" | "absent"
    detail: str


def evaluate_checks(check_runs: Sequence[dict], statuses: Sequence[dict], required: str = REQUIRED_CHECK) -> Gate:
    """Decide green/red/pending from the check runs + commit statuses of ONE sha.

    Green needs the aggregate required check to exist AND be ``success``, and no
    other completed check run or commit status to be failing.  Checks that are
    still running after the aggregate finished (timing report, image build) do
    not block; any failure among them does.
    """
    failed = [f"{c['name']}={c.get('conclusion')}" for c in check_runs if c.get("status") == "completed" and c.get("conclusion") in BAD]
    failed += [f"{s['context']}={s['state']}" for s in statuses if s.get("state") in ("failure", "error")]
    if failed:
        return Gate("red", "failing: " + ", ".join(sorted(failed)))
    agg = [c for c in check_runs if c.get("name") == required]
    if not agg:
        return Gate("absent", f"required check {required!r} has not reported ({len(check_runs)} check runs so far)")
    best = agg[-1]
    if best.get("status") != "completed":
        return Gate("pending", f"required check {required!r} is {best.get('status')}")
    if best.get("conclusion") != "success":
        return Gate("red", f"required check {required!r} concluded {best.get('conclusion')}")
    pending = sorted(c["name"] for c in check_runs if c.get("status") != "completed")
    note = f"; {len(pending)} advisory check(s) still running: {', '.join(pending[:4])}" if pending else ""
    return Gate("green", f"{required!r} success on {len(check_runs)} check runs{note}")


def fetch_gate(ctx: Ctx, sha: str, required: str) -> Gate:
    raw = ctx.out("gh", "api", f"repos/{ctx.repo}/commits/{sha}/check-runs?per_page=100", "--paginate", "--jq", ".check_runs[]")
    runs = [json.loads(line) for line in raw.splitlines() if line.strip()]
    status = ctx.api(f"repos/{ctx.repo}/commits/{sha}/status")
    return evaluate_checks(runs, (status or {}).get("statuses", []), required)


def wait_for_green(ctx: Ctx, pr: int, sha: str, required: str, timeout_s: int, poll_s: int, no_ci_timeout_s: int) -> None:
    """Block until ``sha`` is green. Raises Abort on red, timeout, or a moved PR head."""
    start = ctx.clock()
    last = ""
    while True:
        head = ctx.out("gh", "pr", "view", str(pr), "-R", ctx.repo, "--json", "headRefOid", "-q", ".headRefOid")
        if head != sha:
            raise Abort(f"PR #{pr} head moved from {sha[:10]} to {head[:10]} while waiting; not merging. Nothing was tagged.")
        gate = fetch_gate(ctx, sha, required)
        line = f"{gate.state}: {gate.detail}"
        if line != last:
            ctx.log(f"    [{int(ctx.clock() - start)}s] {sha[:10]} {line}")
            last = line
        if gate.state == "green":
            return
        if gate.state == "red":
            raise Abort(f"CI is red on {sha[:10]} ({gate.detail}). PR #{pr} left open; nothing merged or tagged.")
        waited = ctx.clock() - start
        if gate.state == "absent" and waited > no_ci_timeout_s:
            raise Abort(
                f"no CI check reported on {sha[:10]} after {int(waited)}s. Pushes and PRs made with the default GITHUB_TOKEN "
                "do not trigger workflows, so release-cut needs a GitHub App token or RELEASE_CUT_TOKEN "
                "(see 'One-time setup' in the release docs). PR left open; nothing merged or tagged."
            )
        if waited > timeout_s:
            raise Abort(f"timed out after {int(waited)}s waiting for CI on {sha[:10]} ({gate.detail}). Nothing merged or tagged.")
        ctx.sleep(poll_s)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def load_summary(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("version", "name", "bullets", "changed_files"):
        if not data.get(key):
            raise Abort(f"{path}: summary missing {key!r}")
    return data


def pr_body(summary: dict) -> str:
    bullets = "\n".join(f"- {b}" for b in summary["bullets"])
    return (
        f"Release notes, version bump to {summary['version']}, release date {summary['release_date_string']}.\n\n"
        f"{bullets}\n\n"
        "Opened by the Release cut workflow. It merges this PR only when the required check is green on this exact "
        "commit, then tags the merge commit and publishes the GitHub release."
    )


def commit_message(summary: dict) -> str:
    return f"release: {summary['name']} (v{summary['version']})"


def remote_branch_exists(ctx: Ctx, branch: str) -> bool:
    return bool(ctx.out("git", "ls-remote", "--heads", "origin", branch))


def remote_tag_exists(ctx: Ctx, tag: str) -> bool:
    return bool(ctx.out("git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"))


def release_exists(ctx: Ctx, tag: str) -> bool:
    return ctx.ok("gh", "release", "view", tag, "-R", ctx.repo)


def describe_token(ctx: Ctx, github_token: Optional[str]) -> str:
    tok = os.environ.get("GH_TOKEN", "")
    if github_token and tok == github_token:
        return "github-token"
    return "elevated" if tok else "none"


# ---------------------------------------------------------------------------
# the flow
# ---------------------------------------------------------------------------


def cut(ctx: Ctx, summary: dict, notes_file: Path, *, base_branch: str, required: str, timeout_s: int,
        poll_s: int, no_ci_timeout_s: int, github_token: Optional[str], probe_sha: Optional[str]) -> int:
    version = summary["version"]
    tag = f"v{version}"
    branch = f"release/c{version}"
    title = commit_message(summary)
    token_kind = describe_token(ctx, github_token)

    ctx.step(f"release {tag} ({summary['name']}) {'[DRY RUN: read-only probes, no writes]' if ctx.dry_run else ''}".rstrip())
    ctx.log(f"files changed by release_cut: {', '.join(summary['changed_files'])}")
    ctx.log(f"notes bullets ({len(summary['bullets'])}):")
    for b in summary["bullets"]:
        ctx.log(f"    - {b}")
    ctx.log(f"token kind: {token_kind}")

    # -- preconditions (read-only, identical in dry-run and real runs) ------
    base_sha = ctx.out("git", "rev-parse", "HEAD")
    remote_main = ctx.out("git", "ls-remote", "origin", f"refs/heads/{base_branch}").split("\t")[0]
    ctx.log(f"checked-out commit {base_sha[:10]}; origin/{base_branch} is {remote_main[:10] or '?'}")
    if remote_main != base_sha:
        msg = f"the checkout ({base_sha[:10]}) is not the tip of origin/{base_branch} ({remote_main[:10]})"
        if not ctx.dry_run:
            raise Abort(f"{msg}; main moved. Re-run.")
        ctx.log(f"NOTE: {msg}. Fine for a dry run from a branch; a real run refuses to start here.")
    if remote_branch_exists(ctx, branch):
        raise Abort(f"branch {branch} already exists on origin; delete or reuse it deliberately, then re-run.")
    if remote_tag_exists(ctx, tag):
        raise Abort(f"tag {tag} already exists on origin.")
    if release_exists(ctx, tag):
        raise Abort(f"a GitHub release for {tag} already exists.")
    ctx.log(f"ok: branch {branch}, tag {tag} and release {tag} are all free")

    if ctx.dry_run:
        sha = probe_sha or base_sha
        gate = fetch_gate(ctx, sha, required)
        ctx.log(f"gate probe on {sha[:10]} (the real gate function, live data): {gate.state}: {gate.detail}")
        if token_kind == "github-token":
            ctx.log("NOTE: this run uses the default GITHUB_TOKEN. A real run would refuse here: PRs/pushes made with it never trigger CI.")
        ctx.would(f"git switch -c {branch}")
        ctx.would(f"git add {' '.join(summary['changed_files'])}")
        ctx.would(f"git -c user.name='{BOT_NAME}' commit -m '{title}'")
        ctx.would(f"git push origin {branch}   (no force)")
        ctx.would(f"gh pr create -R {ctx.repo} --base {base_branch} --head {branch} --title '{title}' --body <notes bullets>")
        ctx.would(f"wait for check {required!r} to be success on the pushed SHA (poll {poll_s}s, give up after {timeout_s}s; "
                  f"red/moved head/no CI after {no_ci_timeout_s}s => stop, PR stays open)")
        ctx.would("gh pr merge <PR> --merge --match-head-commit <pushed SHA>")
        ctx.would(f"verify merge commit parents == [{base_sha[:10]}, <pushed SHA>] and its __version__ == {version}")
        ctx.would(f"git tag -a {tag} <merge commit> -m '{summary['name']}' && git push origin refs/tags/{tag}")
        ctx.would(f"gh release create {tag} --verify-tag --title '{summary['name']}' --notes-file <notes bullets> --latest")
        ctx.step("dry run finished: nothing was pushed, merged, tagged or published")
        return 0

    if token_kind == "github-token":
        raise Abort(
            "refusing to run with the default GITHUB_TOKEN: a release PR it opens would never get CI, so the merge gate could "
            "not be satisfied. Configure the GitHub App (APP_CLIENT_ID + APP_PRIVATE_KEY) or RELEASE_CUT_TOKEN as described "
            "under 'One-time setup' in the release docs."
        )
    if token_kind == "none":
        raise Abort("GH_TOKEN is not set.")

    # -- 1-3 branch, commit, push, PR ---------------------------------------
    ctx.step(f"create {branch} and commit the release")
    ctx.out("git", "switch", "-c", branch)
    ctx.out("git", "add", *summary["changed_files"])
    staged = set(ctx.out("git", "diff", "--cached", "--name-only").splitlines())
    if staged != set(summary["changed_files"]):
        raise Abort(f"staged files {sorted(staged)} != expected {sorted(summary['changed_files'])}")
    ctx.out("git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-q", "-m", title)
    sha = ctx.out("git", "rev-parse", "HEAD")
    ctx.step(f"push {branch} ({sha[:10]})")
    ctx.out("git", "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
    ctx.step("open the release PR")
    pr_url = ctx.out("gh", "pr", "create", "-R", ctx.repo, "--base", base_branch, "--head", branch, "--title", title, "--body", pr_body(summary))
    pr = int(pr_url.rstrip("/").rsplit("/", 1)[1])
    ctx.log(f"PR #{pr}: {pr_url}")
    head = ctx.out("gh", "pr", "view", str(pr), "-R", ctx.repo, "--json", "headRefOid", "-q", ".headRefOid")
    if head != sha:
        raise Abort(f"PR head {head[:10]} is not the pushed commit {sha[:10]}")

    # -- 4 wait for green on THIS sha ---------------------------------------
    ctx.step(f"wait for {required!r} on {sha[:10]}")
    wait_for_green(ctx, pr, sha, required, timeout_s, poll_s, no_ci_timeout_s)

    # -- 5 merge --------------------------------------------------------------
    ctx.step("check the PR is still based on the tip of main")
    behind = ctx.api(f"repos/{ctx.repo}/compare/{base_branch}...{sha}").get("behind_by")
    if behind != 0:
        raise Abort(f"main moved by {behind} commit(s) since the release was prepared, so the tested commit is not what would ship. "
                    f"PR #{pr} left open; re-run the workflow.")
    ctx.step(f"merge PR #{pr} (match-head-commit {sha[:10]})")
    ctx.out("gh", "pr", "merge", str(pr), "-R", ctx.repo, "--merge", "--match-head-commit", sha)
    merge = ctx.out("gh", "pr", "view", str(pr), "-R", ctx.repo, "--json", "state,mergeCommit", "-q", '.state + " " + .mergeCommit.oid')
    state, _, merge_sha = merge.partition(" ")
    if state != "MERGED" or len(merge_sha) != 40:
        raise Abort(f"PR #{pr} is not merged (state={state})")
    parents = ctx.api(f"repos/{ctx.repo}/commits/{merge_sha}")["parents"]
    parent_shas = [p["sha"] for p in parents]
    recovery = (
        f"RECOVERY (merged, not tagged): git fetch origin && git tag -a {tag} {merge_sha} -m '{summary['name']}' && "
        f"git push origin refs/tags/{tag} && gh release create {tag} --verify-tag --title '{summary['name']}' --notes-file <bullets>"
    )
    if parent_shas != [base_sha, sha]:
        raise Abort(f"merge commit {merge_sha[:10]} has parents {[p[:10] for p in parent_shas]}, expected [{base_sha[:10]}, {sha[:10]}]. "
                    f"NOT tagging: the merged tree is not the tested one. Decide by hand. {recovery}")

    # -- 6 tag ---------------------------------------------------------------
    try:
        ctx.step(f"tag {tag} on merge commit {merge_sha[:10]}")
        ctx.out("git", "fetch", "-q", "origin", base_branch)
        got = ctx.out("git", "show", f"{merge_sha}:clover_cli/__init__.py")
        if f'__version__ = "{version}"' not in got:
            raise Abort(f"merge commit {merge_sha[:10]} does not contain __version__ = \"{version}\"")
        ctx.out("git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "tag", "-a", tag, merge_sha, "-m", summary["name"])
        ctx.out("git", "push", "origin", f"refs/tags/{tag}")
        # -- 7 release -------------------------------------------------------
        ctx.step(f"publish GitHub release {tag}")
        ctx.out("gh", "release", "create", tag, "-R", ctx.repo, "--verify-tag", "--title", summary["name"],
                "--notes-file", str(notes_file), "--latest")
        url = ctx.out("gh", "release", "view", tag, "-R", ctx.repo, "--json", "url", "-q", ".url")
    except Abort as exc:
        raise Abort(f"{exc}\n{recovery}") from exc
    tag_target = ctx.out("git", "rev-parse", f"{tag}^{{commit}}")
    if tag_target != merge_sha:
        raise Abort(f"{tag} points at {tag_target[:10]}, not {merge_sha[:10]}")
    ctx.step(f"done: {url}  (tag {tag} -> {merge_sha[:10]})")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0] if __doc__ else "")
    p.add_argument("--summary", required=True, help="summary JSON written by release_cut.py --summary-json")
    p.add_argument("--notes-file", required=True, help="release body written by release_cut.py --notes-out")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--base-branch", default="main")
    p.add_argument("--required-check", default=REQUIRED_CHECK)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--probe-sha", help="dry run only: evaluate the check gate against this commit")
    p.add_argument("--timeout-min", type=int, default=90)
    p.add_argument("--poll-s", type=int, default=30)
    p.add_argument("--no-ci-timeout-min", type=int, default=10, help="give up if no check reports at all within this time")
    p.add_argument("--github-token-env", default="DEFAULT_GITHUB_TOKEN", help="env var holding the default GITHUB_TOKEN, to detect a non-elevated token")
    args = p.parse_args(argv)
    if not args.repo:
        print("error: --repo or GITHUB_REPOSITORY required", file=sys.stderr)
        return 2
    root = Path(args.repo_root).resolve()
    ctx = Ctx(repo=args.repo, run=default_runner(root), dry_run=args.dry_run)
    try:
        return cut(
            ctx, load_summary(Path(args.summary)), Path(args.notes_file).resolve(),
            base_branch=args.base_branch, required=args.required_check, timeout_s=args.timeout_min * 60,
            poll_s=args.poll_s, no_ci_timeout_s=args.no_ci_timeout_min * 60,
            github_token=os.environ.get(args.github_token_env) or None, probe_sha=args.probe_sha,
        )
    except Abort as exc:
        print(f"::error::{exc}" if os.environ.get("GITHUB_ACTIONS") else f"ABORT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

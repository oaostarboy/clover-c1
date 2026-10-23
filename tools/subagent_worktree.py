"""Opt-in git worktree isolation for delegated subagents.

Inspired by Muse Code's ``--subagent-worktree-isolation`` (Meta, Aug 2026):
when isolation is on, each delegated child agent gets its own git worktree
checked out from the parent's current commit, so parallel children never
contend for the same working copy and the parent's checkout stays untouched.
This is a clean-room implementation of the documented behavior
(https://dev.meta.ai/docs/muse-code/extending#multi-agent); no Muse Code
code was referenced.

Enable in config.yaml::

    delegation:
      worktree_isolation: true   # default: false

Contract (mirrors Muse Code's documented semantics):

- **Opt-in and git-only.** In a non-git workspace the setting is ignored
  without an error and children share the parent's working directory,
  exactly as before.
- **One worktree per child**, branched from the parent repo's current
  ``HEAD`` under ``<repo>/.worktrees/subagent-<id>`` on branch
  ``clover-subagent/<id>``.
- **The parent reviews/merges.** Children commit inside their own worktree;
  each result entry reports the worktree path, branch, commit count, and
  dirty state so the parent can review or merge each branch.
- **Clean worktrees are pruned.** A worktree with no new commits and a
  clean tree is removed automatically after the child finishes; anything
  holding work is kept and reported. Pruning requires affirmative proof:
  if a git inspection probe fails the state is unknown, so the worktree is
  kept and the result entry carries ``inspection_failed`` + ``note`` (#88113).

Only the local terminal backend is supported: on docker/ssh/modal/etc. the
worktree created on the host would not be visible inside the sandbox, so
isolation is skipped (with a debug log) rather than half-applied.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30
_WORKTREES_DIRNAME = ".worktrees"
_BRANCH_NAMESPACE = "clover-subagent"


def _run_git(args, cwd: str, timeout: int = _GIT_TIMEOUT):
    """Run a git command, capturing output. Never raises on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def local_backend_active() -> bool:
    """True when the terminal backend is local (worktrees visible to tools)."""
    try:
        from clover_cli.config import load_config_readonly

        cfg = load_config_readonly()
        backend = ((cfg.get("terminal") or {}).get("backend") or "local")
        return str(backend).strip().lower() in ("", "local")
    except Exception:
        # Legacy entry points without the shared loader default to local.
        return True


def resolve_repo_root(path: Optional[str]) -> Optional[str]:
    """Return the git toplevel for *path*, or None when not in a work tree."""
    if not path:
        return None
    try:
        candidate = os.path.abspath(os.path.expanduser(str(path)))
    except Exception:
        return None
    if not os.path.isdir(candidate):
        return None
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=candidate)
    except Exception as exc:
        logger.debug("subagent worktree: rev-parse failed: %s", exc)
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def is_confirmed_non_git_workspace(path: Optional[str]) -> bool:
    """True only when *path* is an existing directory with no git checkout.

    Conservative: a missing/unknown path, a ``.git`` entry anywhere above it
    (even one git refuses to open, e.g. dubious ownership), or a git failure
    we cannot classify all return False, so required isolation keeps refusing.
    """
    if not path:
        return False
    try:
        candidate = os.path.abspath(os.path.expanduser(str(path)))
    except Exception:
        return False
    if not os.path.isdir(candidate) or resolve_repo_root(candidate):
        return False
    try:
        real = os.path.realpath(candidate)
    except Exception:
        return False
    # A directory alias (symlink) can hide the real ``.git`` ancestor, so both
    # the spelled path and the resolved path must be free of one.
    for start in dict.fromkeys((candidate, real)):
        if _has_git_entry_above(start) is not False:
            return False
    return True


NO_CHECKOUT_REASON = "workspace is not a git repository, so there is nothing to isolate"


def default_terminal_folder() -> Optional[str]:
    """Folder the gateway/CLI process itself sits in (the last-resort start folder).

    Deliberately not ``TERMINAL_CWD``: cron rewrites that process-global for
    the length of a job, so it is not evidence of where this chat works.
    """
    try:
        return os.getcwd()
    except OSError:
        return None


def format_isolation_refusal(reason: Optional[str], *, conflict: bool = False) -> str:
    """Human-readable refusal: what failed and the exact key that changes it."""
    what = str(reason or "unknown setup failure")
    if conflict:
        fix = (
            "Set delegation.worktree_isolation to true "
            "(clover config set delegation.worktree_isolation true) or set "
            "delegation.worktree_isolation_required to false."
        )
    else:
        fix = (
            "To let helpers run in the shared workspace when isolation fails, run: "
            "clover config set delegation.worktree_isolation_required false "
            "(delegation.worktree_isolation false turns isolation off entirely)."
        )
    return (
        "Required workspace isolation was not available; child was not started: "
        f"{what}. {fix}"
    )


def _has_git_entry_above(start: str) -> Optional[bool]:
    """True/False if a ``.git`` entry exists at/above *start*; None if unreadable."""
    current = start
    while True:
        try:
            os.lstat(os.path.join(current, ".git"))
            return True
        except FileNotFoundError:
            pass
        except NotADirectoryError:
            pass
        except OSError:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


# ── Whose folder is this? ────────────────────────────────────────────────
#
# A subagent's private copy must be built from the PARENT's own folder,
# never from another agent's. The parent's live terminal cwd is not proof of
# ownership: a parent that had just ``cd``-ed into a sibling agent's live
# checkout (to read it) once got its child's worktree created inside that
# sibling's repo. These helpers decide whether a folder belongs to another
# agent and, if so, where the sandbox should be built instead.

_DEFAULT_FOREIGN_AGENT_ROOTS = ("~/agents",)


def _real(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    except Exception:
        return None


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def own_clover_home() -> Optional[str]:
    """This agent's CLOVER_HOME (realpath), or None when it cannot be read."""
    try:
        from clover_constants import get_clover_home

        return _real(str(get_clover_home()))
    except Exception:
        return None


def own_install_repo() -> Optional[str]:
    """The git checkout this agent's own code runs from, if any."""
    return resolve_repo_root(str(Path(__file__).resolve().parent.parent))


def foreign_agent_roots() -> list:
    """Folders that hold other agents' installs (config-overridable)."""
    roots = list(_DEFAULT_FOREIGN_AGENT_ROOTS)
    try:
        from clover_cli.config import load_config_readonly

        cfg = (load_config_readonly() or {}).get("delegation") or {}
        configured = cfg.get("foreign_agent_roots")
        if isinstance(configured, (list, tuple)):
            roots = [str(r) for r in configured if r]
    except Exception:
        pass
    return [r for r in (_real(r) for r in roots) if r]


def _looks_like_clover_home(path: str) -> bool:
    """A folder that is some agent's CLOVER_HOME (has its gateway/state)."""
    try:
        if os.path.exists(os.path.join(path, "gateway.pid")):
            return True
        return os.path.isfile(os.path.join(path, "config.yaml")) and (
            os.path.exists(os.path.join(path, "state.db"))
        )
    except Exception:
        return False


def foreign_owner(path: Optional[str]) -> Optional[str]:
    """Why *path* belongs to another agent, or None when it is ours/neutral.

    Foreign means: inside another agent's CLOVER_HOME, or inside a folder
    under the configured agent-install roots (``~/agents`` by default) that
    is not this agent's own install checkout or home.
    """
    real = _real(path)
    if not real:
        return None
    own_home = own_clover_home()
    own_repo = _real(own_install_repo())
    if own_repo and _is_within(real, own_repo):
        return None
    if own_home and _is_within(real, own_home):
        # Our own home — unless a nested profile home (another agent) holds it.
        current = real
        while current and current != own_home:
            if _looks_like_clover_home(current):
                return f"another agent's home ({current})"
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return None
    current = real
    while True:
        if _looks_like_clover_home(current):
            return f"another agent's home ({current})"
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for root in foreign_agent_roots():
        if _is_within(real, root) and real != root:
            return f"another agent's install folder (under {root})"
    return None


def session_start_candidates() -> list:
    """The parent's own starting folders, most specific first.

    Gateway: the folder frozen at startup. CLI / config: ``terminal.cwd`` and
    the startup ``TERMINAL_CWD``. Last: the checkout this agent's own code
    runs from — "the agent's own folder".
    """
    out: list = []
    try:
        from tools.terminal_tool import get_gateway_start_cwd

        out.append(get_gateway_start_cwd())
    except Exception:
        pass
    try:
        from clover_cli.config import load_config_readonly

        out.append(((load_config_readonly() or {}).get("terminal") or {}).get("cwd"))
    except Exception:
        pass
    out.append(os.environ.get("TERMINAL_CWD"))
    out.append(own_install_repo())
    seen: list = []
    for c in out:
        r = _real(c) if isinstance(c, str) and c.strip() else None
        if r and os.path.isdir(r) and r not in seen:
            seen.append(r)
    return seen


def choose_sandbox_base(
    parent_cwd: Optional[str], start_candidates: Optional[list] = None
) -> tuple[Optional[str], Optional[str]]:
    """Pick the folder a child's sandbox is built from.

    Returns ``(base, redirect_note)``. *base* is *parent_cwd* when that folder
    is the parent's own (or neutral); when it belongs to another agent, the
    first non-foreign git repo among the parent's session start folders. If
    none qualifies, *base* is ``None`` and the caller uses a private scratch
    folder. *redirect_note* explains a redirect (None when not redirected).
    """
    owner = foreign_owner(parent_cwd)
    if owner is None:
        return parent_cwd, None
    candidates = (
        start_candidates if start_candidates is not None
        else session_start_candidates()
    )
    for cand in candidates:
        if foreign_owner(cand) is None and resolve_repo_root(cand):
            if foreign_owner(resolve_repo_root(cand)) is None:
                return cand, (
                    f"the parent's current folder {parent_cwd} is {owner}; "
                    f"the sandbox was built from the parent's own repo {cand} instead"
                )
    return None, (
        f"the parent's current folder {parent_cwd} is {owner}, and no own "
        "repo was found; a private scratch folder is used instead"
    )


def create_scratch_sandbox(subagent_id: Optional[str]) -> Optional[Dict[str, str]]:
    """A private, non-git folder for a child whose parent sat in a foreign repo."""
    short_id = (subagent_id or uuid.uuid4().hex[:8]).replace("/", "-")
    home = own_clover_home()
    base = Path(home) / "sandboxes" if home else Path(
        os.environ.get("TMPDIR", "/tmp")
    )
    path = base / f"subagent-{short_id}"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("subagent sandbox: cannot create %s: %s", path, exc)
        return None
    return {"path": str(path), "branch": "", "repo_root": "", "base_commit": "",
            "scratch": "1"}


def _ensure_gitignore_entry(repo_root: str) -> None:
    """Best-effort: keep ``.worktrees/`` out of git status."""
    gitignore = Path(repo_root) / ".gitignore"
    entry = f"{_WORKTREES_DIRNAME}/"
    try:
        existing = (
            gitignore.read_text(encoding="utf-8-sig", errors="replace")
            if gitignore.exists()
            else ""
        )
        if entry not in existing.splitlines():
            with open(gitignore, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(f"{entry}\n")
    except Exception as exc:
        logger.debug("subagent worktree: could not update .gitignore: %s", exc)


def create_subagent_worktree(
    parent_cwd: Optional[str],
    subagent_id: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Create an isolated worktree for one child agent.

    Returns metadata (``path``, ``branch``, ``repo_root``, ``base_commit``)
    on success, or ``None`` when the workspace is not a git repository or
    worktree creation fails — mirroring Muse Code, absence of git downgrades
    silently to shared-workspace behavior.
    """
    repo_root = resolve_repo_root(parent_cwd)
    if not repo_root:
        return None

    short_id = (subagent_id or uuid.uuid4().hex[:8]).replace("/", "-")
    wt_name = f"subagent-{short_id}"
    branch = f"{_BRANCH_NAMESPACE}/{wt_name}"
    wt_path = Path(repo_root) / _WORKTREES_DIRNAME / wt_name

    try:
        wt_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("subagent worktree: cannot create %s: %s", wt_path.parent, exc)
        return None

    _ensure_gitignore_entry(repo_root)

    try:
        base = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
        base_commit = base.stdout.strip() if base.returncode == 0 else ""
        result = _run_git(
            ["worktree", "add", str(wt_path), "-b", branch, "HEAD"],
            cwd=repo_root,
        )
    except Exception as exc:
        logger.warning("subagent worktree: creation failed: %s", exc)
        return None
    if result.returncode != 0:
        # Common on repos with zero commits (unborn HEAD) — degrade silently.
        logger.warning(
            "subagent worktree: git worktree add failed: %s",
            result.stderr.strip(),
        )
        return None

    logger.info("subagent worktree created: %s (branch %s)", wt_path, branch)
    return {
        "path": str(wt_path),
        "branch": branch,
        "repo_root": repo_root,
        "base_commit": base_commit,
    }


def mark_worktree_payload_unproven(
    payload: Dict[str, Any], reason: str, *, unmeasured: str = "commits/dirty"
) -> Dict[str, Any]:
    """Flag a worktree result payload as un-inspected, in place (#88113).

    A failed probe proves nothing about the tree, so the fields it would have
    filled keep their defaults. The parent agent only ever sees this dict — it
    cannot read logs — so the uncertainty has to travel *in the payload*, or
    "0 commits, clean" reads as "the child produced nothing" and the work we
    just preserved is never looked at.

    *unmeasured* names only the fields this failure actually left unproven: one
    probe can succeed while the other fails (a bad ``base_commit`` fails
    ``rev-list`` while ``status`` still reports a real ``dirty``), and claiming
    a measured value is UNKNOWN would be its own kind of misreport.

    Shared by ``finalize_subagent_worktree`` and ``delegate_tool``'s
    finalize-raised fallback so the two producers of this schema cannot drift.
    """
    path = payload.get("path", "")
    branch = payload.get("branch", "")
    payload["inspection_failed"] = True
    payload["note"] = (
        f"git inspection failed ({reason}): {unmeasured} UNKNOWN — not "
        "proven zero/clean. The worktree and branch were preserved "
        f"— inspect {path} (branch {branch}) before assuming no work."
    )
    logger.warning(
        "subagent worktree: git inspection failed (%s) — keeping %s "
        "(branch %s) for manual review",
        reason,
        path,
        branch,
    )
    return payload


def unproven_worktree_payload(
    info: Dict[str, str], reason: str
) -> Dict[str, Any]:
    """Build a complete un-inspected payload from creation-side *info*.

    For callers that never got a payload back at all (``delegate_tool``'s
    fallback when ``finalize_subagent_worktree`` itself raises). Emits exactly
    the schema the parent expects — notably WITHOUT the creation-side
    ``repo_root``/``base_commit`` internals.
    """
    return mark_worktree_payload_unproven(
        {
            "path": info.get("path", ""),
            "branch": info.get("branch", ""),
            "commits": 0,
            "dirty": False,
            "pruned": False,
        },
        reason,
    )


def finalize_subagent_worktree(
    info: Dict[str, str], *, prune: bool = True
) -> Dict[str, Any]:
    """Inspect (and possibly prune) a child worktree after the child finishes.

    Returns a result-entry payload: path, branch, ``commits`` ahead of the
    base, ``dirty`` (uncommitted changes present), and ``pruned``. A worktree
    with zero commits and a clean tree is removed when *prune* is true **and
    both git probes succeeded**; anything holding work is always kept for the
    parent to review or merge.

    If ``git rev-list``/``git status`` exits non-zero (or the inspection
    raises), the tree state is unknown, so the worktree and branch are kept
    and the payload carries ``inspection_failed: True`` plus a ``note``.
    ``commits``/``dirty`` are then defaults, NOT measurements — the parent
    must inspect the worktree instead of concluding the child did no work.
    """
    path = info.get("path", "")
    branch = info.get("branch", "")
    repo_root = info.get("repo_root", "")
    base_commit = info.get("base_commit", "")

    payload: Dict[str, Any] = {
        "path": path,
        "branch": branch,
        "commits": 0,
        "dirty": False,
        "pruned": False,
    }
    if not path or not os.path.isdir(path):
        payload["pruned"] = True  # nothing on disk to review
        return payload

    if info.get("scratch"):
        # Private non-git scratch folder: no commits to count. Empty → remove;
        # anything the child wrote is kept for the parent to look at.
        try:
            has_files = any(os.scandir(path))
        except Exception:
            has_files = True
        payload["dirty"] = bool(has_files)
        payload["scratch"] = True
        if prune and not has_files:
            try:
                os.rmdir(path)
                payload["pruned"] = True
            except Exception as exc:
                logger.debug("subagent scratch: prune failed: %s", exc)
        return payload

    def _unproven(
        reason: str, *, unmeasured: str = "commits/dirty"
    ) -> Dict[str, Any]:
        return mark_worktree_payload_unproven(
            payload, reason, unmeasured=unmeasured
        )

    # A worktree whose commit count was never measured must not be pruned
    # either: the prune condition reads payload["commits"], and without a base
    # commit that value is an unproven default, exactly the class of bug
    # #88113 is about.
    if not base_commit:
        return _unproven(
            "no base_commit recorded — commit count unmeasurable",
            unmeasured="commits",
        )

    failed: list = []
    unmeasured: list = []
    try:
        counted = _run_git(
            ["rev-list", "--count", f"{base_commit}..HEAD"], cwd=path
        )
        if counted.returncode == 0:
            payload["commits"] = int(counted.stdout.strip() or 0)
        else:
            failed.append(
                f"rev-list exit {counted.returncode}: "
                f"{counted.stderr.strip()[:200]}"
            )
            unmeasured.append("commits")
        status = _run_git(["status", "--porcelain"], cwd=path)
        if status.returncode == 0:
            payload["dirty"] = bool(status.stdout.strip())
        else:
            failed.append(
                f"status exit {status.returncode}: "
                f"{status.stderr.strip()[:200]}"
            )
            unmeasured.append("dirty")
    except Exception as exc:
        # Same unknown state as a non-zero exit (timeout, OSError, or a
        # non-numeric rev-list stdout) — keep the worktree rather than risk
        # deleting work, and tell the caller the numbers are unproven. Which
        # probe raised is unknowable here, so neither value is trustworthy.
        return _unproven(f"inspection raised: {exc}")

    if failed:
        # Fail-safe (#88113): a destructive cleanup requires affirmative proof
        # of "zero commits + clean tree"; the defaults prove nothing.
        return _unproven(
            "; ".join(failed), unmeasured="/".join(unmeasured)
        )

    if prune and payload["commits"] == 0 and not payload["dirty"]:
        try:
            removed = _run_git(
                ["worktree", "remove", "--force", path], cwd=repo_root or path
            )
            if removed.returncode == 0:
                _run_git(["branch", "-D", branch], cwd=repo_root or path)
                payload["pruned"] = True
                logger.info("subagent worktree pruned (no work): %s", path)
            else:
                logger.debug(
                    "subagent worktree: prune failed: %s", removed.stderr.strip()
                )
        except Exception as exc:
            logger.debug("subagent worktree: prune failed: %s", exc)

    return payload


def prepare_subagent_worktree(
    parent_cwd: Optional[str],
    subagent_id: Optional[str],
    *,
    enabled: bool,
    required: bool,
    local_backend: bool,
) -> tuple[Dict[str, Any], Optional[Dict[str, str]]]:
    """Resolve the actual workspace outcome before a child can start editing."""
    if not enabled:
        return {
            "status": "shared",
            "required": required,
            "reason": "worktree isolation is disabled",
        }, None
    if not local_backend:
        return {
            "status": "blocked" if required else "shared",
            "required": required,
            "reason": "worktree isolation is unsupported by the active terminal backend",
        }, None
    # Never build a sandbox inside another agent's checkout or home. When the
    # parent's live terminal sits in one, build from the parent's own start
    # repo instead, or fall back to a private scratch folder.
    original_cwd = parent_cwd
    base, redirect = choose_sandbox_base(parent_cwd)
    if redirect is not None:
        logger.warning("subagent sandbox redirected: %s", redirect)
        if base is None:
            scratch = create_scratch_sandbox(subagent_id)
            if scratch is not None:
                scratch["redirected_from"] = str(original_cwd)
            if scratch is None:
                return {
                    "status": "blocked" if required else "shared",
                    "required": required,
                    "reason": redirect + " (scratch folder creation failed)",
                }, None
            return {
                "status": "isolated",
                "required": required,
                "path": scratch["path"],
                "branch": "",
                "scratch": True,
                "redirected_from": parent_cwd,
                "reason": redirect,
            }, scratch
        parent_cwd = base
    # A parent with no recorded folder (e.g. a chat restored after a gateway
    # restart) runs its commands where the process started. If THAT is a
    # confirmed non-git folder there is still nothing to isolate.
    probe = parent_cwd or default_terminal_folder()
    if not resolve_repo_root(parent_cwd):
        if is_confirmed_non_git_workspace(probe):
            # Isolation protects a git checkout from concurrent writers; with
            # no checkout there is nothing to protect, so even required
            # isolation lets the child run in the parent's workspace.
            return {
                "status": "shared",
                "required": required,
                "reason": NO_CHECKOUT_REASON,
                "no_checkout": True,
            }, None
        return {
            "status": "blocked" if required else "shared",
            "required": required,
            "reason": "workspace is not a git repository"
            if parent_cwd else "the parent's working folder is unknown",
        }, None
    try:
        info = create_subagent_worktree(parent_cwd, subagent_id)
    except Exception as exc:
        info = None
        reason = f"worktree setup failed: {type(exc).__name__}: {exc}"
    else:
        reason = "git worktree creation failed"
    if info is None:
        return {
            "status": "blocked" if required else "shared",
            "required": required,
            "reason": reason,
        }, None
    outcome: Dict[str, Any] = {
        "status": "isolated",
        "required": required,
        "path": info.get("path", ""),
        "branch": info.get("branch", ""),
    }
    if redirect is not None:
        info["redirected_from"] = str(original_cwd)
        outcome["reason"] = redirect
    return outcome, info


def build_shared_workspace_context_note(outcome: Dict[str, Any]) -> str:
    """Tell a child before execution that it will share, not isolate, files."""
    if outcome.get("no_checkout"):
        return (
            "\n\n[WORKSPACE ISOLATION OUTCOME] This task is running in the "
            "parent's workspace, NOT an isolated worktree: the workspace is "
            "not a git repository, so there is nothing to isolate. Do not "
            "claim isolation; avoid concurrent edits to the same files."
        )
    return (
        "\n\n[WORKSPACE ISOLATION OUTCOME] This task is running in the shared "
        "workspace, NOT an isolated worktree. Reason: "
        f"{outcome.get('reason', 'not provided')}. Avoid concurrent edits to the "
        "same files; do not claim isolation."
    )


_READS_ALLOWED_NOTE = (
    "This rule limits WRITES only. Reading is allowed on any path: read "
    "files, ls, du, grep, find, git log/show/diff/status, and inspect any "
    "folder the task names (including the main checkout), from anywhere. "
    "If the task asks you to measure or read another path, read it there."
)


def build_worktree_context_note(info: Dict[str, str]) -> str:
    """Context block telling the child where it may write.

    Restricts only writes (edits, new files, installs, git mutations) to the
    child's own copy. Reads anywhere are explicitly allowed: a read-only job
    on the main checkout must not be refused because of this note.
    """
    redirected = info.get("redirected_from")
    redirect_line = (
        f"(Your sandbox was built away from the parent's current folder: {redirected}.)\n"
        if redirected else ""
    )
    if info.get("scratch"):
        return (
            "\n\n[SANDBOX] Your private scratch folder is: "
            f"{info.get('path')}\n{redirect_line}"
            "Write only inside this folder: file edits, new files, installs "
            "and builds go here, and nowhere else. Do not edit, install into, "
            "or run git commands that change any other folder. "
            + _READS_ALLOWED_NOTE
        )
    return (
        "\n\n[WORKTREE ISOLATION] You are working in an isolated git worktree "
        f"at: {info.get('path')}\n"
        f"Your dedicated branch is: {info.get('branch')}\n{redirect_line}"
        "Write only inside this worktree (your terminal already starts "
        "there): file edits, new files, builds, and git commits, checkouts, "
        "resets or stashes happen here. Do not change the main repository "
        "checkout or any other folder, and never pip/uv install into another "
        "agent's venv (make your own with `uv venv .venv` here). "
        + _READS_ALLOWED_NOTE
        + " Commit your changes to your branch when done; the parent agent "
        "will review and merge your branch. If you make no commits and leave "
        "the tree clean, the worktree is discarded automatically."
    )

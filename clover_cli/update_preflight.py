"""Read-only update pre-check: say why an install can't update BEFORE anything changes.

``/update`` (chat) and ``clover update`` (terminal) used to find out halfway
through that an install could not update: a refusal after the gateway was
already paused, a crash mid-pull, or a cryptic git error. This module runs a
handful of fast, read-only checks first. Each one returns a
:class:`PreflightCheck` with:

* ``status`` — ``ok`` / ``warn`` / ``block``
* ``code`` — a stable identifier (safe to match on; never reworded)
* ``reason`` — one plain-English sentence for a non-technical user
* ``fix`` — the exact thing to do about it (empty for ``ok``)

A ``block`` stops the update before it touches anything; a ``warn`` lets it
proceed with a short note. The checks NEVER mutate the install, never prompt,
and never raise: a check that cannot run reports ``ok`` with a "skipped"
reason, so the pre-check can only add refusals backed by positive evidence.

Wherever the updater already has the detection logic, the check calls it
(``clover_cli.update_cmd`` parked-branch assessment, venv-holder scan and its
reap rungs, fetch-failure classifier; ``clover_cli.update_lock`` marker
semantics) so the pre-check and the real update cannot disagree about what
they see.

Settings live in ``config.yaml`` under ``updates.preflight`` (see
:func:`preflight_settings`). ``clover update --skip-preflight`` bypasses the
whole thing for one run.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
BLOCK = "block"

#: Exit code for a pre-check refusal: the updater's existing "declined to
#: proceed, nothing changed" convention (``update_contract.UPDATE_EXIT_REFUSED``).
PREFLIGHT_EXIT_BLOCKED = 2

OFFICIAL_REPO_URL = "https://github.com/oaostarboy/clover-c1.git"

_DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    # Below this much free space the update is refused; below warn_free_disk_mb
    # it proceeds with a note. A dependency reinstall can need several hundred MB.
    "min_free_disk_mb": 500,
    "warn_free_disk_mb": 1500,
    # One `git ls-remote` against origin. Read-only; a timeout is only a note.
    "check_network": True,
    "network_timeout_seconds": 15,
}


@dataclass(frozen=True)
class PreflightCheck:
    """One pre-check verdict."""

    code: str
    status: str  # ok | warn | block
    reason: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class PreflightReport:
    """All verdicts from one pre-check run."""

    checks: list[PreflightCheck] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def blocks(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == BLOCK]

    @property
    def warnings(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def blocked(self) -> bool:
        return bool(self.blocks)

    def codes(self, status: Optional[str] = None) -> list[str]:
        return [c.code for c in self.checks if status is None or c.status == status]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "duration_s": round(self.duration_s, 3),
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def preflight_settings(config: Optional[dict] = None) -> dict[str, Any]:
    """Resolve ``updates.preflight`` from config.yaml over the defaults.

    Malformed values fall back to the default for that key. Never raises.
    """
    settings = dict(_DEFAULT_SETTINGS)
    try:
        if config is None:
            from clover_cli.config import load_config

            config = load_config() or {}
        updates = config.get("updates") if isinstance(config, dict) else None
        raw = updates.get("preflight") if isinstance(updates, dict) else None
        if isinstance(raw, bool):
            # `updates.preflight: false` is accepted as shorthand for enabled: false.
            settings["enabled"] = raw
        elif isinstance(raw, dict):
            for key, default in _DEFAULT_SETTINGS.items():
                if key not in raw:
                    continue
                value = raw[key]
                if isinstance(default, bool):
                    if isinstance(value, bool):
                        settings[key] = value
                    elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
                        settings[key] = value.strip().lower() == "true"
                else:
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        continue
                    if number >= 0:
                        settings[key] = int(number) if isinstance(default, int) else number
    except Exception as exc:
        logger.debug("Could not read updates.preflight: %s", exc)
    return settings


# ---------------------------------------------------------------------------
# Small read-only helpers
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    return sys.platform == "win32"


def _git(root: Path, *args: str, timeout: float = 15.0, env: Optional[dict] = None):
    """Run a read-only git probe; ``None`` on spawn failure or timeout.

    ``--no-optional-locks`` stops ``git status`` from opportunistically
    rewriting ``.git/index`` (its stat-cache refresh), so a probe never
    writes inside the checkout.
    """
    from clover_cli._subprocess_compat import bounded_probe_run, noninteractive_git_env

    return bounded_probe_run(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        timeout=timeout,
        env=env if env is not None else noninteractive_git_env(),
    )


@contextmanager
def _no_optional_git_locks():
    """``GIT_OPTIONAL_LOCKS=0`` for git calls made by reused updater helpers.

    The updater's own probes (e.g. ``_assess_parked_branch_switch``) run plain
    ``git status``, which may refresh ``.git/index``. Disabling optional locks
    only skips that cache write; results are identical. A value the user set
    is left alone.
    """
    if "GIT_OPTIONAL_LOCKS" in os.environ:
        yield
        return
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        yield
    finally:
        os.environ.pop("GIT_OPTIONAL_LOCKS", None)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(_resolve(a))) == os.path.normcase(str(_resolve(b)))


def _plain(text: str) -> str:
    """Strip the CLI's leading status glyph from a reused updater message."""
    return text.strip().lstrip("✗⚠✓ ").strip()


def _cli_main():
    """``clover_cli.main`` when already loaded (the CLI), else ``None``.

    Inside a gateway the entry module is ``__main__``; importing the 600 KB
    CLI module just for a pre-check is avoided where the helper also lives in
    ``clover_cli.update_cmd``.
    """
    return sys.modules.get("clover_cli.main")


def _default_project_root() -> Path:
    main_mod = _cli_main()
    if main_mod is not None and getattr(main_mod, "PROJECT_ROOT", None):
        return Path(main_mod.PROJECT_ROOT)
    from clover_cli._startup_fast import project_root_str

    return Path(project_root_str())


def _default_clover_home() -> Path:
    from clover_cli.config import get_clover_home

    return Path(get_clover_home())


def _status_entries(root: Path) -> Optional[list[str]]:
    """``git status --porcelain`` paths, minus npm lockfile churn.

    The updater discards ``package-lock.json`` churn (a lockfile changed while
    its ``package.json`` is not) before it looks at the tree
    (``update_cmd._discard_lockfile_churn``), so such entries must not count
    as user changes here either.
    """
    result = _git(root, "status", "--porcelain")
    if result is None or result.returncode != 0:
        return None
    paths = []
    for line in (result.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append((line[:2], path))
    dirty_package_dirs = {
        str(Path(p).parent) for xy, p in paths if p.endswith("package.json") and xy.strip() == "M"
    }
    out = []
    for xy, p in paths:
        if (
            p.endswith("package-lock.json")
            and xy.strip() == "M"
            and str(Path(p).parent) not in dirty_package_dirs
        ):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Checks. Each returns a list of verdicts (usually one) and never raises:
# run_update_preflight() wraps every call.
# ---------------------------------------------------------------------------


def check_update_in_progress(clover_home: Optional[Path] = None) -> list[PreflightCheck]:
    """Another live update holds the shared update marker.

    Same semantics as ``UpdateLock.acquire``: a dead/expired marker is no
    holder, and a marker owned by our own orchestrating parent (Desktop
    updater hand-off) is ours. Unlike ``read_live_update`` this never deletes
    a stale marker.
    """
    from clover_cli import update_lock

    marker = (Path(clover_home) / update_lock.MARKER_NAME) if clover_home else update_lock.update_marker_path()
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [PreflightCheck("no_update_running", OK, "No other update is running.")]
    try:
        pid = int(lines[0].strip())
    except (IndexError, ValueError):
        pid = -1
    try:
        started = float(lines[1].strip())
    except (IndexError, ValueError):
        started = float("-inf")
    age = time.time() - started
    if (
        pid <= 0
        or age > update_lock.UPDATE_MARKER_MAX_AGE_SECONDS
        or not update_lock._pid_alive(pid)
        or pid == update_lock._handoff_pid()
        or update_lock._is_ancestor_pid(pid)
        or pid == os.getpid()
    ):
        return [PreflightCheck("no_update_running", OK, "No other update is running.")]
    minutes = max(int(age // 60), 0)
    when = f"{minutes} minute(s) ago" if minutes else "less than a minute ago"
    return [
        PreflightCheck(
            "update_in_progress",
            BLOCK,
            f"Another Clover update is already running (started {when}), and a second one at the same time could break the install.",
            "Wait a few minutes for it to finish, then try again.",
        )
    ]


def _git_install_hint() -> str:
    if _is_windows():
        return "Install Git for Windows from https://git-scm.com/download/win, then try again."
    if sys.platform == "darwin":
        return "Run: xcode-select --install   (this installs Git), then try again."
    return "Install Git with your package manager (for example: sudo apt install git), then try again."


def check_git_checkout(root: Path, clover_home: Path) -> list[PreflightCheck]:
    """Git available; the install is a usable checkout (or adoptable copy)."""
    git_dir = root / ".git"
    git_bin = shutil.which("git")
    if not git_dir.exists():
        if git_bin is None:
            if _is_windows():
                return [
                    PreflightCheck(
                        "not_git_checkout",
                        WARN,
                        "This install is a plain copy of the files and Git isn't installed, so the update will download the new version as a ZIP file instead.",
                    )
                ]
            return [
                PreflightCheck(
                    "git_missing",
                    BLOCK,
                    "This install is a plain copy of the files and Git isn't installed, so the update has no way to download the new version.",
                    _git_install_hint(),
                )
            ]
        return [
            PreflightCheck(
                "not_git_checkout",
                WARN,
                "This install is a plain copy of the files (no Git history), so the update will first set it up for updates and then download the new version.",
            )
        ]
    if git_bin is None:
        return [
            PreflightCheck(
                "git_missing",
                BLOCK,
                "Git isn't installed or can't be found, so the update can't download the new version.",
                _git_install_hint(),
            )
        ]
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if head is None:
        return [PreflightCheck("git_checkout", OK, "Git checkout (HEAD check timed out; skipped).")]
    if head.returncode != 0:
        detail = ((head.stderr or "").strip().splitlines() or ["no commits"])[0]
        return [
            PreflightCheck(
                "git_broken",
                BLOCK,
                f"The install's Git data is damaged or empty ({detail}), so the update can't tell which version you have.",
                f'Rename the folder "{root}" (for example to "{root.name}.broken"), then re-run the Clover installer for your system. '
                f'Your chats, memories and settings live in "{clover_home}" and are kept.',
            )
        ]
    return [PreflightCheck("git_checkout", OK, "The install is a Git checkout.")]


def check_origin_remote(root: Path) -> list[PreflightCheck]:
    if not (root / ".git").exists():
        return []
    result = _git(root, "remote", "get-url", "origin")
    if result is None:
        return []
    if result.returncode != 0 or not (result.stdout or "").strip():
        return [
            PreflightCheck(
                "no_origin_remote",
                BLOCK,
                "This install has no download source (Git remote 'origin') set, so the update doesn't know where to get new versions.",
                f'Run: git -C "{root}" remote add origin {OFFICIAL_REPO_URL}   then try again.',
            )
        ]
    return [PreflightCheck("origin_remote", OK, f"Downloads from {(result.stdout or '').strip()}.")]


def check_install_writable(root: Path) -> list[PreflightCheck]:
    """The install folder and its Git data can be written by this user.

    ``os.access`` only — nothing is created. (On Windows ``os.access`` sees
    only the read-only attribute, not ACLs, so this catches less there.)
    """
    candidates: list[Path] = [root]
    git_dir = root / ".git"
    if git_dir.is_dir():
        candidates += [git_dir, git_dir / "objects", git_dir / "refs", git_dir / "index", git_dir / "HEAD"]
        objects = git_dir / "objects"
        try:
            candidates += [p for p in objects.iterdir() if p.is_dir()]
        except OSError:
            pass
    for name in ("clover_cli", "gateway", "agent", "tools"):
        candidates.append(root / name)
    for path in candidates:
        if not path.exists():
            continue
        if not os.access(path, os.W_OK):
            if _is_windows():
                fix = (
                    f'Make sure "{root}" isn\'t read-only and that you\'re signed in as the user who installed Clover, then try again.'
                )
            else:
                fix = f'Give your user ownership back, then try again. Run: sudo chown -R "$(id -un)" "{root}"'
            return [
                PreflightCheck(
                    "install_read_only",
                    BLOCK,
                    f'Clover isn\'t allowed to change "{path}", so the update couldn\'t replace its files.',
                    fix,
                )
            ]
    return [PreflightCheck("install_writable", OK, "The install folder is writable.")]


def check_disk_space(root: Path, settings: dict) -> list[PreflightCheck]:
    try:
        free_mb = shutil.disk_usage(str(root)).free // (1024 * 1024)
    except OSError:
        return []
    minimum = int(settings.get("min_free_disk_mb", 0) or 0)
    warn_at = int(settings.get("warn_free_disk_mb", 0) or 0)
    if minimum and free_mb < minimum:
        return [
            PreflightCheck(
                "low_disk_space",
                BLOCK,
                f"Only {free_mb} MB of disk space is free where Clover is installed, and an update needs at least {minimum} MB.",
                "Free up some disk space, then try again.",
            )
        ]
    if warn_at and free_mb < warn_at:
        return [
            PreflightCheck(
                "disk_space_low",
                WARN,
                f"Disk space is getting low ({free_mb} MB free); the update should still fit.",
            )
        ]
    return [PreflightCheck("disk_space", OK, f"{free_mb} MB free.")]


def _current_branch(root: Path) -> Optional[str]:
    result = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if result is None or result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _parked_strategy_in_place() -> bool:
    try:
        from clover_cli.config import load_config

        cfg = (load_config() or {}).get("updates", {})
        return isinstance(cfg, dict) and cfg.get("parked_branch_strategy", "switch") == "update_in_place"
    except Exception:
        return False


def check_branch_and_changes(
    root: Path, branch: str, *, switch_branch: bool = False, gateway_mode: bool = False
) -> list[PreflightCheck]:
    """Wrong branch (the updater's parked-branch guard) and local edits.

    The parked-branch verdict comes from ``update_cmd._assess_parked_branch_switch``
    itself, so this predicts exactly what the updater will do after its fetch:
    a dirty parked branch or ``auto_switch_parked_branch: false`` is skipped
    with exit 1 — the case that refused an update on 2026-10-07.
    """
    if not (root / ".git").exists():
        return []
    current = _current_branch(root)
    if current is None:
        return []
    entries = _status_entries(root)
    out: list[PreflightCheck] = []

    if current not in (branch, "HEAD"):
        # The updater's own guard (it calls it through clover_cli.main).
        helpers = _cli_main()
        if helpers is None or not hasattr(helpers, "_assess_parked_branch_switch"):
            from clover_cli import update_cmd as helpers
        _safe, reason = helpers._assess_parked_branch_switch(["git"], root, current, branch)
        if reason == "dirty" and entries == []:
            # Only npm lockfile churn, which the updater discards first.
            reason = "unverifiable"
        if reason == "dirty":
            n = len(entries or [])
            out.append(
                PreflightCheck(
                    "parked_branch_dirty",
                    BLOCK,
                    f"This install is on the branch '{current}' instead of '{branch}' and has {n} unsaved change(s), so the update won't switch branches and risk your edits.",
                    f'Save the changes and switch back, then try again. Run: git -C "{root}" stash push --include-untracked -m before-update && git -C "{root}" checkout {branch}'
                    "   (your changes stay saved in git stash).",
                )
            )
            return out
        if reason == "disabled":
            out.append(
                PreflightCheck(
                    "parked_branch_locked",
                    BLOCK,
                    f"This install is on the branch '{current}' instead of '{branch}', and config.yaml (updates.auto_switch_parked_branch: false) says not to switch it automatically.",
                    f'Run: git -C "{root}" checkout {branch}   or set updates.auto_switch_parked_branch to true, then try again.',
                )
            )
            return out
        if reason.startswith("unmerged:"):
            count = reason.split(":", 1)[1]
            if _parked_strategy_in_place() and not switch_branch:
                out.append(
                    PreflightCheck(
                        "parked_branch_in_place",
                        WARN,
                        f"This install is on the branch '{current}' with {count} commit(s) of its own; the update will merge '{branch}' into it instead of switching.",
                    )
                )
            else:
                out.append(
                    PreflightCheck(
                        "parked_branch_unmerged",
                        WARN,
                        f"This install is on the branch '{current}' with {count} commit(s) that aren't in '{branch}'; the update will switch to '{branch}' and keep those commits safe on '{current}'.",
                    )
                )
        elif reason == "unverifiable":
            out.append(
                PreflightCheck(
                    "parked_branch_unverified",
                    WARN,
                    f"This install is on the branch '{current}' instead of '{branch}'; the update will check it after downloading and switch back if that's safe.",
                )
            )
        else:
            out.append(
                PreflightCheck(
                    "parked_branch_merged",
                    OK,
                    f"On '{current}', which is fully merged; the update will switch back to '{branch}'.",
                )
            )
    else:
        out.append(PreflightCheck("branch", OK, f"On '{current if current != 'HEAD' else 'a detached commit'}'."))

    if entries:
        discard = False
        if gateway_mode or not (sys.stdin.isatty() and sys.stdout.isatty()):
            try:
                from clover_cli.config import load_config

                cfg = (load_config() or {}).get("updates", {})
                discard = isinstance(cfg, dict) and str(cfg.get("non_interactive_local_changes", "stash")).lower() == "discard"
            except Exception:
                discard = False
        if discard:
            reason_text = (
                f"There are {len(entries)} changed file(s) in the Clover program folder, and config.yaml "
                "(updates.non_interactive_local_changes: discard) says to throw them away after a successful update."
            )
        else:
            reason_text = (
                f"There are {len(entries)} changed file(s) in the Clover program folder; the update will set them aside and put them back afterwards"
                + (" (it may ask you here first)." if gateway_mode else ".")
            )
        out.append(PreflightCheck("local_changes", WARN, reason_text))
    elif entries is not None:
        out.append(PreflightCheck("clean_tree", OK, "No local changes."))
    return out


def check_shallow(root: Path, branch: str, *, switch_branch: bool = False) -> list[PreflightCheck]:
    """Shallow (installer ``--depth 1``) checkouts.

    On the target branch the updater copes: when ``merge --ff-only`` fails it
    resets to the remote. The shape it cannot finish is an in-place update of
    a custom branch (``updates.parked_branch_strategy: update_in_place``)
    whose partial history shares no commit with ``origin/<branch>``:
    ``git merge`` then stops with "refusing to merge unrelated histories"
    after the update already paused the gateway, stashed and tagged. That is
    detected read-only from the refs the last fetch left behind.
    """
    if not (root / ".git").exists():
        return []
    result = _git(root, "rev-parse", "--is-shallow-repository")
    if result is None or result.returncode != 0:
        return []
    if (result.stdout or "").strip() != "true":
        return []
    current = _current_branch(root)
    if current not in (None, branch, "HEAD") and _parked_strategy_in_place() and not switch_branch:
        target = _git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
        if target is not None and target.returncode == 0:
            base = _git(root, "merge-base", "HEAD", f"refs/remotes/origin/{branch}")
            if base is not None and base.returncode == 1:
                return [
                    PreflightCheck(
                        "shallow_unrelated_history",
                        BLOCK,
                        f"This install was downloaded without its full history, so Git can't merge '{branch}' into its own branch '{current}' (the two histories don't connect).",
                        f'Download the full history once, then try again. Run: git -C "{root}" fetch --unshallow origin',
                    )
                ]
    return [PreflightCheck("shallow_clone", OK, "Partial-history download (the installer default); handled automatically.")]


def _clover_trees(pythonpath: str) -> list[Path]:
    """PYTHONPATH entries that are Clover source trees.

    Entries inside a virtualenv/site-packages are never trees: a Windows
    gateway adds its own venv's site-packages to PYTHONPATH
    (``gateway.run._ensure_windows_gateway_venv_imports``).
    """
    found: list[Path] = []
    for entry in (pythonpath or "").split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        low = entry.replace("\\", "/").lower()
        if "site-packages" in low or "dist-packages" in low:
            continue
        candidate = Path(entry)
        try:
            if (candidate / "clover_cli" / "__init__.py").is_file():
                found.append(candidate)
        except OSError:
            continue
    return found


def editable_dir_from_direct_url(text: str) -> Optional[Path]:
    """The checkout an editable install points at, from PEP 610 ``direct_url.json``."""
    import json
    from urllib.parse import unquote, urlparse
    from urllib.request import url2pathname

    try:
        data = json.loads(text or "")
        if not (data.get("dir_info") or {}).get("editable"):
            return None
        parsed = urlparse(str(data.get("url") or ""))
        if parsed.scheme != "file":
            return None
        path = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc != "localhost":
            path = "//" + parsed.netloc + path
        return Path(path) if path else None
    except Exception:
        return None


def installed_checkout() -> Optional[Path]:
    """Where this interpreter's Clover install lives (editable installs only).

    Only an existing Clover git checkout counts: a stale record (an install
    folder that was moved) is not evidence of anything.
    """
    try:
        from importlib import metadata

        for name in ("clover-c1", "clover_c1"):
            try:
                dist = metadata.distribution(name)
            except metadata.PackageNotFoundError:
                continue
            # importlib.metadata.Distribution.read_text (UTF-8 internally), not Path.
            path = editable_dir_from_direct_url(dist.read_text("direct_url.json") or "")  # windows-footgun: ok
            if path is not None and (path / "clover_cli" / "__init__.py").is_file() and (path / ".git").exists():
                return path
            return None
    except Exception:
        return None
    return None


def _running_gateway_pythonpath() -> str:
    """PYTHONPATH of this profile's live gateway, when readable. Never raises."""
    try:
        from gateway.status import get_running_pid

        pid = get_running_pid()
        if not pid or int(pid) == os.getpid():
            return ""
        import psutil  # type: ignore

        return str(psutil.Process(int(pid)).environ().get("PYTHONPATH", "") or "")
    except Exception:
        return ""


_UNSET: Any = object()


def check_trial_build(
    root: Path,
    *,
    env_pythonpath: Optional[str] = None,
    gateway_pythonpath: Optional[str] = None,
    installed_root: Any = _UNSET,
) -> list[PreflightCheck]:
    """A trial (canary) build loaded through PYTHONPATH.

    Positive evidence only — a PYTHONPATH entry must be a Clover source tree
    and differ from the real install:

    * the running code (*root*) is a PYTHONPATH tree while the interpreter's
      editable install points at a different checkout: /update would pull
      into the trial tree (typically a feature branch) instead of the real
      install — the refusal Clover itself hit on 2026-10-07;
    * the live gateway imports another Clover tree via PYTHONPATH: the
      update would change this install while the gateway keeps running the
      trial code, and the post-update version check then fails.
    """
    own = env_pythonpath if env_pythonpath is not None else os.environ.get("PYTHONPATH", "")
    gw = gateway_pythonpath if gateway_pythonpath is not None else _running_gateway_pythonpath()
    installed = installed_checkout() if installed_root is _UNSET else installed_root
    trial: Optional[Path] = None
    real: Optional[Path] = None
    for tree in _clover_trees(own):
        if _same_path(tree, root):
            if installed is not None and not _same_path(installed, root):
                trial, real = tree, installed
                break
        else:
            trial, real = tree, root
            break
    if trial is None:
        for tree in _clover_trees(gw):
            if not _same_path(tree, root):
                trial, real = tree, root
                break
    if trial is None:
        return []
    if _is_windows():
        fix = "Remove the PYTHONPATH environment variable for Clover and restart it, then try again."
    elif sys.platform == "darwin":
        fix = "Remove PYTHONPATH from the Clover LaunchAgent (launchctl print gui/$(id -u)/ai.clover.gateway shows it) and restart Clover, then try again."
    else:
        fix = "Remove the PYTHONPATH setting (systemctl --user cat clover-gateway shows any drop-in that sets it), restart Clover, then try again."
    return [
        PreflightCheck(
            "trial_build_active",
            BLOCK,
            f'Clover is running a trial build from "{trial}" (loaded with PYTHONPATH) instead of your install at "{real}", so /update would not update the code the assistant actually runs.',
            fix,
        )
    ]


def classify_venv_holders(
    holders: list[tuple[int, str, str]],
    *,
    is_pausable_gateway: Callable[[str], bool],
    reap_rungs: Iterable[Callable[[list[tuple[int, str, str]]], Any]] = (),
    ancestors_of: Optional[Callable[[int], set[int]]] = None,
    extra_gateway_pids: Iterable[int] = (),
) -> list[tuple[int, str, str]]:
    """Holders the updater would still refuse on after its own clean-up rungs.

    Pure function (the Windows-only process scan is done by the caller):

    * gateways are paused by the updater, and so are their child processes
      (a gateway drains and exits, taking its children with it) — a holder
      whose ancestry (``ancestors_of``) contains a gateway PID is not a
      blocker. ``extra_gateway_pids`` names gateways not in *holders*, e.g.
      the calling gateway itself when /update runs the check in-process;
    * each ``reap_rungs`` entry returns the PIDs (or ledger entries with
      ``pid``) the updater would stop itself, or ``None`` when it would stop
      none.
    """
    gateway_pids = {int(h[0]) for h in holders if is_pausable_gateway(h[2])}
    gateway_pids.update(int(p) for p in extra_gateway_pids)
    remaining = []
    for holder in holders:
        if int(holder[0]) in gateway_pids:
            continue
        if ancestors_of is not None and gateway_pids:
            try:
                if ancestors_of(int(holder[0])) & gateway_pids:
                    continue
            except Exception:
                pass
        remaining.append(holder)
    for rung in reap_rungs:
        if not remaining:
            break
        try:
            picked = rung(remaining)
        except Exception:
            picked = None
        if not picked:
            continue
        pids = set()
        for item in picked:
            if isinstance(item, dict):
                try:
                    pids.add(int(item.get("pid")))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    pids.add(int(item))
                except (TypeError, ValueError):
                    continue
        remaining = [h for h in remaining if int(h[0]) not in pids]
    return remaining


def check_windows_locks(*, force: bool = False, force_venv: bool = False, gateway_mode: bool = False) -> list[PreflightCheck]:
    """Windows: other Clover programs holding files the update must replace.

    Mirrors the updater's own two refusals (concurrent ``clover.exe``; venv
    holders after its pause/reap rungs) so the user hears about them before
    the gateway is paused, not after.
    """
    if not _is_windows():
        return []
    # Every helper is looked up on clover_cli.main, as the updater does
    # (`_m().<helper>`), so both always run the same detection code.
    from clover_cli import main as _main

    out: list[PreflightCheck] = []
    if not force:
        scripts_dir = _main._venv_scripts_dir()
        if scripts_dir is not None:
            concurrent = _main._detect_concurrent_clover_instances(scripts_dir)
            if concurrent:
                non_gateway = _main._filter_non_gateway_concurrent_instances(concurrent)
                if non_gateway:
                    out.append(
                        PreflightCheck(
                            "clover_exe_running",
                            BLOCK,
                            f"{len(non_gateway)} other Clover window(s) are using the program files, so Windows won't let the update replace them.",
                            "Close other Clover terminal windows and the Clover desktop app, then try again.",
                        )
                    )
    if not force_venv:
        holders = _main._detect_venv_python_processes()
        if holders:
            # Desktop GUI-updater hand-off (`update --gateway --force` with the
            # update marker claimed): the updater reaps leftover backends in
            # that context instead of refusing, so do not predict a refusal.
            handoff = False
            try:
                handoff = gateway_mode and force and _main._update_marker_path().exists()
            except Exception:
                handoff = False
            from clover_cli._scan_venv_blockers import _is_pausable_gateway

            def _ancestors(pid: int) -> set[int]:
                import psutil  # type: ignore

                return {int(a.pid) for a in psutil.Process(pid).parents()}

            own_gateway: list[int] = []
            try:
                import psutil  # type: ignore

                if _is_pausable_gateway(" ".join(psutil.Process().cmdline() or [])):
                    own_gateway.append(os.getpid())
            except Exception:
                pass
            remaining = classify_venv_holders(
                holders,
                is_pausable_gateway=_is_pausable_gateway,
                reap_rungs=(
                    _main._ledger_reapable_backend_pids,
                    _main._orphaned_desktop_backend_pids,
                    _main._ledger_manual_serve_holders,
                ),
                ancestors_of=_ancestors,
                extra_gateway_pids=own_gateway,
            )
            if remaining and not handoff:
                names = ", ".join(f"PID {pid} ({name})" for pid, name, _cmd in remaining[:4])
                more = f" and {len(remaining) - 4} more" if len(remaining) > 4 else ""
                out.append(
                    PreflightCheck(
                        "venv_in_use",
                        BLOCK,
                        f"Other Clover programs are using the files the update must replace: {names}{more}.",
                        "Close the Clover desktop app and any other Clover windows (a running chat gateway is fine; it is paused automatically), then try again.",
                    )
                )
    if not out:
        out.append(PreflightCheck("windows_files_free", OK, "No other Clover program is holding the files."))
    return out


def check_network(root: Path, branch: str, settings: dict) -> list[PreflightCheck]:
    """One read-only ``git ls-remote`` against origin for the target branch."""
    if not settings.get("check_network", True) or not (root / ".git").exists():
        return []
    from clover_cli._subprocess_compat import noninteractive_git_env

    env = noninteractive_git_env()
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")
    timeout = float(settings.get("network_timeout_seconds", 15) or 15)
    result = _git(root, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}", timeout=timeout, env=env)
    if result is None:
        return [
            PreflightCheck(
                "network_slow",
                WARN,
                f"GitHub didn't answer within {int(timeout)} seconds; the update will try anyway.",
            )
        ]
    if result.returncode == 0:
        return [PreflightCheck("network", OK, "GitHub is reachable.")]
    if result.returncode == 2:
        return [
            PreflightCheck(
                "branch_not_on_origin",
                BLOCK,
                f"The branch '{branch}' doesn't exist on the download source, so there is nothing to update to.",
                "Check the branch name, or update without --branch to get the normal release.",
            )
        ]
    stderr = (result.stderr or "").strip()
    first = (stderr.splitlines() or [""])[0]
    low = stderr.lower()
    from clover_cli.update_cmd import _classify_fetch_failure

    diagnosis = _plain(_classify_fetch_failure(stderr))
    if (
        "authentication failed" in low
        or "could not read username" in low
        or "permission denied" in low
        or "host key verification failed" in low
        or "terminal prompts disabled" in low
    ):
        # The probe never prompts; an interactive fetch with a passphrase or
        # credential prompt may still succeed, so this is only a note.
        return [
            PreflightCheck(
                "network_auth_unconfirmed",
                WARN,
                "Clover couldn't confirm access to the download source without asking for a password; the update will try anyway.",
            )
        ]
    return [
        PreflightCheck(
            "network_unreachable",
            BLOCK,
            f"Clover can't reach the download source right now ({diagnosis.rstrip('.')}{': ' + first if first else ''}), so it can't download the update.",
            "Check the internet connection, then try again in a few minutes.",
        )
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_update_preflight(
    *,
    project_root: Optional[Path] = None,
    clover_home: Optional[Path] = None,
    branch: str = "main",
    gateway_mode: bool = False,
    force: bool = False,
    force_venv: bool = False,
    switch_branch: bool = False,
    settings: Optional[dict] = None,
    include_network: bool = True,
) -> PreflightReport:
    """Run every check and return the report. Read-only; never raises."""
    started = time.monotonic()
    report = PreflightReport()
    try:
        root = Path(project_root) if project_root is not None else _default_project_root()
    except Exception as exc:
        logger.debug("Pre-check could not resolve the install root: %s", exc)
        return report
    try:
        home = Path(clover_home) if clover_home is not None else _default_clover_home()
    except Exception:
        home = root
    if settings is None:
        settings = preflight_settings()

    steps: list[tuple[str, Callable[[], list[PreflightCheck]]]] = [
        ("update_lock", lambda: check_update_in_progress(clover_home if clover_home is not None else None)),
        ("git", lambda: check_git_checkout(root, home)),
        ("origin", lambda: check_origin_remote(root)),
        ("writable", lambda: check_install_writable(root)),
        ("disk", lambda: check_disk_space(root, settings)),
        ("branch", lambda: check_branch_and_changes(root, branch, switch_branch=switch_branch, gateway_mode=gateway_mode)),
        ("shallow", lambda: check_shallow(root, branch, switch_branch=switch_branch)),
        ("trial", lambda: check_trial_build(root)),
        ("windows", lambda: check_windows_locks(force=force, force_venv=force_venv, gateway_mode=gateway_mode)),
    ]
    with _no_optional_git_locks():
        for name, step in steps:
            try:
                report.checks.extend(step())
            except Exception as exc:
                logger.debug("Update pre-check %s failed: %s", name, exc)
                report.checks.append(PreflightCheck(f"{name}_unchecked", OK, f"Skipped ({type(exc).__name__})."))
            # A broken/absent checkout makes every later git check meaningless.
            if name == "git" and any(c.code in ("git_broken", "git_missing") for c in report.checks):
                break
        if (
            include_network
            and not report.blocked
            and not any(c.code in ("git_broken", "git_missing") for c in report.checks)
        ):
            try:
                report.checks.extend(check_network(root, branch, settings))
            except Exception as exc:
                logger.debug("Update pre-check network failed: %s", exc)
    report.duration_s = time.monotonic() - started
    return report


def record_block_receipt(report: PreflightReport) -> None:
    """Persist a ``refused`` update receipt for a pre-check block.

    Same contract as ``update_contract.record_refusal_receipt``: fleet tooling
    sees the attempted-and-refused update instead of silence. Only the
    receipt directory under CLOVER_HOME is written, never the install.
    Never raises.
    """
    try:
        from clover_cli.update_receipt import (
            begin_update_receipt,
            finalize_update_receipt,
            record_step,
        )

        begin_update_receipt()
        for check in report.blocks:
            record_step(f"preflight:{check.code}", False, check.reason)
        finalize_update_receipt(
            "refused", stop_reason="preflight: " + ",".join(report.codes(BLOCK))
        )
    except Exception as exc:
        logger.debug("Could not record pre-check refusal receipt: %s", exc)


def gate_cli_update(args: Any, *, gateway_mode: bool) -> Optional[PreflightReport]:
    """Pre-check for the ``clover update`` apply path.

    Returns ``None`` when skipped (``--skip-preflight`` or
    ``updates.preflight.enabled: false``), else the report. Prints the block
    message or the warning notes; the caller exits on ``report.blocked``.
    """
    if getattr(args, "skip_preflight", False):
        return None
    settings = preflight_settings()
    if not settings.get("enabled", True):
        return None
    branch = (getattr(args, "branch", None) or "main").strip() or "main"
    report = run_update_preflight(
        branch=branch,
        gateway_mode=gateway_mode,
        force=bool(getattr(args, "force", False)),
        force_venv=bool(getattr(args, "force_venv", False)),
        switch_branch=bool(getattr(args, "switch_branch", False)),
        settings=settings,
    )
    try:
        if report.blocked:
            print(format_cli_block(report))
            record_block_receipt(report)
        elif report.warnings:
            print(format_cli_warnings(report))
            print()
    except Exception:
        pass
    return report


def chat_check_requested(args_text: str) -> bool:
    """``/update check`` (or ``/update --check``) asks for the report only."""
    return (args_text or "").strip().lower() in ("check", "--check", "precheck", "pre-check")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

SKIP_HINT_CLI = "To skip the pre-check in an emergency: clover update --skip-preflight"


def format_cli_block(report: PreflightReport) -> str:
    lines = ["✗ Update not started: the pre-check found a problem. Nothing was changed.", ""]
    for check in report.blocks:
        lines.append(f"  • {check.reason}")
        if check.fix:
            lines.append(f"    Fix: {check.fix}")
        lines.append(f"    (code: {check.code})")
        lines.append("")
    lines.append(f"  {SKIP_HINT_CLI}")
    return "\n".join(lines)


def format_cli_warnings(report: PreflightReport) -> str:
    return "\n".join(f"⚠ Pre-check: {c.reason} ({c.code})" for c in report.warnings)


_ICONS = {OK: "✓", WARN: "⚠", BLOCK: "✗"}


def format_cli_report(report: PreflightReport) -> str:
    if report.blocked:
        head = "✗ Update pre-check: this install can't update right now."
    elif report.warnings:
        head = "✓ Update pre-check: ready to update (with notes)."
    else:
        head = "✓ Update pre-check: ready to update."
    lines = [head]
    for check in report.checks:
        lines.append(f"  {_ICONS.get(check.status, '•')} [{check.code}] {check.reason}")
        if check.fix and check.status != OK:
            lines.append(f"      Fix: {check.fix}")
    return "\n".join(lines)


def format_chat_block(report: PreflightReport) -> str:
    parts = ["⚠️ Update not started — nothing was changed."]
    for check in report.blocks:
        entry = check.reason
        if check.fix:
            entry += f"\nFix: {check.fix}"
        entry += f"\n(code: {check.code})"
        parts.append(entry)
    return "\n\n".join(parts)


def format_chat_warnings(report: Optional[PreflightReport]) -> str:
    if report is None or not report.warnings:
        return ""
    return "\n".join(f"Note: {c.reason}" for c in report.warnings)


def format_chat_report(report: PreflightReport) -> str:
    icons = {OK: "✅", WARN: "⚠️", BLOCK: "⛔"}
    if report.blocked:
        head = "🔎 Update pre-check: this install can't update right now."
    elif report.warnings:
        head = "🔎 Update pre-check: ready to update, with notes. Send /update to start."
    else:
        head = "🔎 Update pre-check: ready to update. Send /update to start."
    lines = [head, ""]
    for check in report.checks:
        lines.append(f"{icons.get(check.status, '•')} {check.reason}")
        if check.fix and check.status != OK:
            lines.append(f"   Fix: {check.fix}")
    return "\n".join(lines)

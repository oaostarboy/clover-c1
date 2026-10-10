"""Ownership proof for a git lock file: is the git that took it still alive?

Adapted from NousResearch/hermes-agent a81d3408bc (MIT) -- hermes_cli/_early_recovery.py.

A git killed while it held .git/index.lock leaves the file behind and every
later merge/stash/reset refuses with "File exists". File age is the wrong test
(a 10-minute floor strands the lock for 10 minutes); the right one is whether
any live process can still own it: Linux reads every process's fds through
/proc, macOS/BSD ask lsof and ps. When this platform cannot prove the
lock dead the answer is "unknown" and the caller never deletes it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path


class _Holder(str):
    """A process that may own a git lock, e.g. ``pid 4242 (git commit)``: truthy, and never ``is True``."""


# Git subcommands that only read: none takes ``index.lock`` and then waits with its fd CLOSED. Every
# other git (``commit``/``merge``/``rebase``/``am``/``stash``... in the editor or a hook, a dashed
# ``git-commit`` from git-core, an alias, a wrapper, a third-party ``git-<tool>``, a git whose
# subcommand cannot be read) may hold ``index.lock`` with no fd naming it, so it counts as a holder.
# A reader (a paged ``git log``, ``cat-file --batch``, a background ``fetch``) counts only while its
# fd is on the lock.
_READER_GIT = frozenset({
    "annotate", "blame", "cat-file", "check-attr", "check-ignore", "check-mailmap", "check-ref-format",
    "cherry", "count-objects", "describe", "diff", "diff-files", "diff-index", "diff-tree",
    "for-each-ref", "fsmonitor--daemon", "grep", "help", "log", "ls-files", "ls-remote", "ls-tree",
    "merge-base", "name-rev", "range-diff", "rev-list", "rev-parse", "shortlog", "show", "show-branch",
    "show-ref", "status", "var", "verify-commit", "verify-tag", "version", "whatchanged",
    "credential", "credential-cache", "credential-store",
    # Transfers write objects and refs, never the index.
    "fetch", "fetch-pack", "http-fetch", "index-pack", "pack-objects", "remote-http", "remote-https",
    "upload-pack",
})
# Global options that take the NEXT argument as their value (``git -C <dir> -c k=v commit``).
_GIT_VALUE_OPTIONS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix", "--attr-source",
})
# What points a git at a repository other than its cwd.
_GIT_LOCATION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def _git_program(arg0: str) -> str | None:
    """``git`` or ``git-<sub>`` when ``arg0`` (an argv[0], a comm, a psutil name) runs git, else None.

    Any directory (``/usr/lib/git-core/git-commit``), either separator, ``.exe`` and case folded:
    Windows runs ``git.exe`` and ``git-commit.exe``."""
    name = re.split(r"[\\/]", arg0.strip())[-1].lower()
    name = name.removesuffix(".exe")
    return name if name == "git" or name.startswith("git-") else None


def _git_argv(cmdline: bytes | list[str]) -> list[str]:
    if isinstance(cmdline, bytes):
        return [a.decode("utf-8", "replace") for a in cmdline.split(b"\0") if a]
    return [str(a) for a in cmdline if a]


def _git_subcommand_of(cmdline: bytes | list[str]) -> str | None:
    """The subcommand a git argv runs: ``commit`` for ``git -C x -c k=v commit`` and for a dashed
    ``/usr/lib/git-core/git-commit`` (``git-commit.exe``); None when argv[0] is not git or names none."""
    args = _git_argv(cmdline)
    program = _git_program(args[0]) if args else None
    if program is None:
        return None
    if program != "git":
        return program[len("git-"):]
    it = iter(args[1:])
    for arg in it:
        if arg in _GIT_VALUE_OPTIONS:
            next(it, None)
        elif not arg.startswith("-"):
            return arg
    return None


def _checkout_places(*paths: Path | str | None) -> tuple[str, ...]:
    """The checkout's own paths as given AND resolved, so another process's path need not be resolved."""
    places = {os.path.normcase(os.path.normpath(os.path.abspath(p))) for p in paths if p is not None}
    places |= {os.path.normcase(os.path.realpath(p)) for p in paths if p is not None}
    return tuple(sorted(places))


def _path_within(path: str, places: tuple[str, ...]) -> bool:
    """``path`` is one of ``places`` or under it, by PATH COMPONENTS (``/x/clover-backup`` is not in ``/x/clover``).

    Lexical: another process's cwd is already the kernel's resolved path, and resolving a stranger's
    path would touch whatever it names."""
    if not os.path.isabs(path):
        return False
    lexical = os.path.normcase(os.path.normpath(path))
    for place in places:
        try:
            if os.path.commonpath([lexical, place]) == place:
                return True
        except ValueError:  # another drive
            continue
    return False


def git_works_in(args: list[str], cwd: str | None, env: dict[str, str] | None, places: tuple[str, ...]) -> bool:
    """A git with this argv/cwd/environment works in ``places``: its cwd, a path argument (absolute,
    or relative to its cwd, ``--opt=<path>`` too) or a ``GIT_DIR``-style variable is inside one."""
    def inside(value: str) -> bool:
        if not value:
            return False
        if not os.path.isabs(value):
            if not cwd:
                return False
            value = os.path.join(cwd, value)
        return _path_within(value, places)

    if cwd and _path_within(cwd, places):
        return True
    for arg in args[1:]:
        if inside(arg.partition("=")[2] if arg.startswith("-") else arg):
            return True
    return any(inside((env or {}).get(key, "")) for key in _GIT_LOCATION_ENV)


def _could_write(uid: int | None, git_dir: Path) -> bool:
    """A process running as ``uid`` may write ``git_dir`` (root, its owner, or a group/world-writable dir)."""
    try:
        st = os.stat(git_dir)
    except OSError:
        return True
    return uid is None or uid in (0, st.st_uid) or bool(st.st_mode & 0o022)


def _held_open(path: Path, root: Path | None = None, *, any_git: bool = False) -> _Holder | bool | None:
    """Whether a running process may still own ``path`` (a git lock): the holder, False, or None (unknowable).

    The holder is a truthy :class:`_Holder` naming it (``pid 4242 (git commit)``). False is proof, not
    a guess: Linux reads every process's fds through /proc and, with ``root``, also counts any live git
    working in the checkout (cwd, a path argument or ``GIT_DIR``) that is not a pure reader
    (:data:`_READER_GIT`; ``any_git`` counts readers too), whatever form runs it: ``git commit``
    waiting in the editor has CLOSED its lock fd, and a dashed ``git-commit``, an alias or ``git.exe``
    is the same git. A git that could write the git dir but whose cwd or environment cannot be read
    makes the answer None. macOS/BSD ask ``lsof`` for open fds and ``ps`` for any such git (``ps``
    cannot say where it works, so any counts). Without either check the answer is None and the caller
    never deletes the lock. Windows needs no answer here: it refuses to unlink a file another process
    has open, so the caller's unlink is the probe.
    """
    if (Path("/proc") / "self" / "fd").is_dir():
        return _held_open_proc(path, root, any_git)
    return _held_open_lsof(path, root, any_git)


# Daemons a git spawns that outlive it, often with the checkout as cwd, and never take the index
# lock: even ``any_git`` ignores them, or one cached credential would keep a dead lock for hours.
_NEVER_LOCKS = frozenset({"credential-cache--daemon", "fsmonitor--daemon"})


def _counts_as_holder(args: list[str], any_git: bool) -> bool:
    sub = _git_subcommand_of(args)
    return sub not in _NEVER_LOCKS and (any_git or sub not in _READER_GIT)


def _held_open_proc(path: Path, root: Path | None, any_git: bool) -> _Holder | bool | None:
    proc = Path("/proc")
    target = os.path.realpath(path)
    places = _checkout_places(root, path.parent) if root is not None else None
    unknowable = False

    def name(args: list[str], pid_dir: Path) -> str:
        if args:
            return " ".join([os.path.basename(args[0]), *args[1:3]])
        try:
            return (pid_dir / "comm").read_bytes().decode("ascii", "replace").strip()  # /proc: Linux only
        except OSError:
            return "?"

    for pid_dir in proc.glob("[0-9]*"):
        try:
            if any(os.readlink(entry.path) == target for entry in os.scandir(pid_dir / "fd")):
                try:
                    args = _git_argv((pid_dir / "cmdline").read_bytes())
                except OSError:
                    args = []
                return _Holder(f"pid {pid_dir.name} ({name(args, pid_dir)})")
        except OSError:
            pass
        if places is None:
            continue
        try:
            uid = pid_dir.stat().st_uid
        except OSError:
            continue  # exited
        try:
            args = _git_argv((pid_dir / "cmdline").read_bytes())
            comm = (pid_dir / "comm").read_bytes().decode("ascii", "replace").strip()
        except OSError:
            if pid_dir.exists() and _could_write(uid, path.parent):
                unknowable = True  # hidden from us (hidepid) yet able to write here: it may be a git
            continue
        if not args:
            continue  # a zombie or a kernel thread holds nothing
        if _git_program(args[0]) is None and _git_program(comm) is None:
            continue
        if int(pid_dir.name) == os.getpid() or not _counts_as_holder(args, any_git):
            continue
        try:
            cwd = os.readlink(pid_dir / "cwd").removesuffix(" (deleted)")
            env = dict(entry.decode("utf-8", "replace").partition("=")[::2]
                       for entry in (pid_dir / "environ").read_bytes().split(b"\0") if entry)
        except OSError:
            if git_works_in(args, None, None, places):
                return _Holder(f"pid {pid_dir.name} ({name(args, pid_dir)})")
            if pid_dir.exists() and _could_write(uid, path.parent):
                unknowable = True  # a lock-keeping git we cannot place may be working here
            continue
        if git_works_in(args, cwd, env, places):
            return _Holder(f"pid {pid_dir.name} ({name(args, pid_dir)})")
    return None if unknowable else False


def _held_open_lsof(path: Path, root: Path | None, any_git: bool) -> _Holder | bool | None:
    import shutil

    lsof = shutil.which("lsof") or next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.isfile(p)), None)
    if lsof is None:
        return None
    try:
        found = subprocess.run([lsof, "-F", "pc", "--", str(path)], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=20,
                               stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [line[1:] for line in found.stdout.splitlines() if line.startswith("p")]
    if pids:
        names = [line[1:] for line in found.stdout.splitlines() if line.startswith("c")]
        return _Holder(f"pid {pids[0]} ({names[0] if names else '?'})")
    if found.returncode not in (0, 1):  # lsof exits 1 when nothing has the file open
        return None
    return False if root is None else _ps_git_holder(lsof, path.parent, root, any_git)


def _lsof_cwds(lsof: str, pids: list[str]) -> dict[str, str] | None:
    """Each pid's cwd from ``lsof -d cwd`` (macOS has no /proc); None when lsof cannot answer."""
    try:
        out = subprocess.run([lsof, "-a", "-d", "cwd", "-F", "pn", "-p", ",".join(pids)], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 1):
        return None
    cwds: dict[str, str] = {}
    pid = None
    for line in out.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid is not None:
            cwds[pid] = line[1:]
    return cwds


def _still_running(pid: int) -> bool:
    """POSIX only (the lsof branch never runs on Windows); stdlib, as this launch-time repair must be."""
    try:
        os.kill(pid, 0)  # windows-footgun: ok — lsof/ps branch is macOS/BSD only, Windows never reaches it
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: alive, another user's
        return True
    return True


def _ps_git_holder(lsof: str, git_dir: Path, root: Path, any_git: bool) -> _Holder | bool | None:
    """An empty ``lsof`` is no proof: ``git commit`` waiting in the editor has closed its lock fd.
    Every git (by executable name, dashed forms included) that could write ``git_dir``, is not a pure
    reader, and works in the checkout (its cwd from ``lsof -d cwd``, or a path argument) keeps the
    lock; one whose cwd cannot be read makes the answer None. Gits elsewhere on the machine do not
    count, or any commit open in another repository would block every launch-time repair."""
    import shutil

    # The launcher can run with a PATH that has neither git nor ps (a Windows-style install, a
    # stripped service env): ps lives at /bin/ps on macOS and every BSD.
    ps_bin = shutil.which("ps") or next((p for p in ("/bin/ps", "/usr/bin/ps") if os.path.isfile(p)), None)
    if ps_bin is None:
        return None

    def ps(*columns: str) -> list[list[str]] | None:
        try:
            out = subprocess.run([ps_bin, "-A", *(f"-o{c}=" for c in columns)], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 timeout=20, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return [line.split(None, len(columns) - 1) for line in out.stdout.splitlines() if line.strip()]

    named = ps("pid", "uid", "comm")  # comm: the executable (``/usr/libexec/git-core/git-commit``)
    commands = ps("pid", "command")
    if named is None or commands is None:
        return None
    argv = {row[0]: row[1].split() for row in commands if len(row) == 2}
    candidates: dict[str, list[str]] = {}
    for row in named:
        if len(row) != 3 or not (row[0].isdigit() and row[1].isdigit()) or int(row[0]) == os.getpid():
            continue
        if _git_program(row[2]) is None and not (argv.get(row[0]) and _git_program(argv[row[0]][0])):
            continue
        args = argv.get(row[0]) or [row[2]]
        if _git_program(args[0]) is None:
            args = [row[2]]  # an argv[0] with spaces: no subcommand, so it counts
        if _could_write(int(row[1]), git_dir) and _counts_as_holder(args, any_git):
            candidates[row[0]] = args
    if not candidates:
        return False
    cwds = _lsof_cwds(lsof, sorted(candidates))
    if cwds is None:
        return None
    places = _checkout_places(root, git_dir)
    for pid, args in candidates.items():
        cwd = cwds.get(pid)
        if cwd is None and _still_running(int(pid)):
            return None  # a lock-keeping git we cannot place may be working here
        if git_works_in(args, cwd, None, places):
            return _Holder(f"pid {pid} ({' '.join([os.path.basename(args[0]), *args[1:3]])})")
    return False


def _release_dead_index_lock(git_dir: Path, root: Path | None = None, *, any_git: bool = False) -> bool:
    """Drop a killed git's ``index.lock`` (it refuses every git command) once its owner is PROVEN gone.

    False while a live git may hold it, or when this platform cannot prove it dead: the caller then
    keeps the interrupted-pull marker, so the next launch tries again instead of a rollback being lost.
    ``any_git``: every git working in ``root`` keeps it, readers included (:func:`_held_open`).
    """
    lock = git_dir / "index.lock"
    deadline = time.monotonic() + 5
    while True:
        examined = _lock_identity(lock)
        if examined is None and not lock.exists():
            return True
        held = None if sys.platform == "win32" else _held_open(lock, root, any_git=any_git)
        if held is False or sys.platform == "win32":
            # The ownership proof above covers the file we examined, not whatever the path names
            # now: a concurrent cleanup plus a live git can swap it in the meantime.
            try:
                if sys.platform == "win32":
                    lock.unlink()
                    return True
                outcome = _unlink_if_same_file(lock, examined)
                if outcome is not None:
                    return outcome
            except FileNotFoundError:
                return True
            except PermissionError:  # Windows: open in a live process
                pass
        elif held is None:
            return False
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)


def _lock_identity(path: Path) -> tuple[int, int, int, int] | None:
    """What a lock file IS (device, inode, size, mtime_ns), or None when the path names nothing."""
    try:
        st = path.lstat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _unlink_if_same_file(lock: Path, examined: tuple[int, int, int, int] | None) -> bool | None:
    """Delete ``lock`` only if it is still the file whose owner was proven gone.

    True: removed (or already gone). None: the path now names a different file, so the proof does not
    cover it and the caller must examine again. False: could not move it aside; leave it alone.

    The path is renamed to a unique quarantine name first (atomic), and the inode re-checked on the
    quarantined file, so a lock swapped in between the re-stat and the rename is put back instead of
    deleted. Putting it back is ``link`` + ``unlink``, which never overwrites a newer lock.
    """
    if examined is None:
        return None
    if _lock_identity(lock) != examined:
        return None
    quarantine = lock.with_name(f"{lock.name}.clover-dead-{os.getpid()}-{time.monotonic_ns()}")
    try:
        os.rename(lock, quarantine)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if _lock_identity(quarantine) == examined:
        try:
            quarantine.unlink()
        except OSError:
            pass
        return True
    # We moved a different file aside (it was replaced after our re-stat): restore it.
    try:
        os.link(quarantine, lock)
    except OSError:
        pass  # a newer lock already exists; the moved file is the live git's old name
    try:
        quarantine.unlink()
    except OSError:
        pass
    return None

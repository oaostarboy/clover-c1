#!/usr/bin/env python3
"""Deterministic version bump for a Clover C1 release.

Turns a written ``RELEASE_NOTES.md`` entry into the exact file edits the past
releases (v1.1.0, v1.1.1, v1.1.2 -- commit 253629ba / PR #76) made by hand:

    clover_cli/__init__.py   __version__ (and __release_date__ when the day changed)
    pyproject.toml           [project] version
    uv.lock                  the project's own ``[[package]]`` entry
    RELEASE_NOTES.md         ONLY when promoting a ``<!-- draft -->`` entry

It then runs ``tests/clover_cli/test_release_notes.py``.  It never commits,
tags, pushes or publishes -- the *Release cut* workflow
(.github/workflows/release-cut.yml + scripts/ci/release_publish.py) does that.

Usage::

    python scripts/release_cut.py 1.1.4                 # explicit version
    python scripts/release_cut.py --bump patch          # 1.1.3 -> 1.1.4
    python scripts/release_cut.py minor --dry-run       # 1.1.3 -> 1.2.0, preview only
    python scripts/release_cut.py 1.1.4 --name "Clover C1.1.4"

Refuses (exit 2) when: the version is not strictly greater than the current
one or than the newest tag; ``vX.Y.Z`` already exists locally or on the remote;
the RELEASE_NOTES entry is missing, a draft with no real bullets, contains
TODO, or has more than 6 bullets; the version files already disagree.
Exit 3 when the release-notes test fails (edits are rolled back).

``--dry-run`` applies the edits transiently so the test sees the post-bump
tree, prints the diff, then restores every file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

INIT_REL = "clover_cli/__init__.py"
PYPROJECT_REL = "pyproject.toml"
LOCK_REL = "uv.lock"
NOTES_REL = "RELEASE_NOTES.md"
DEFAULT_TEST_CMD = (
    f"{shlex.quote(sys.executable)} -m pytest tests/clover_cli/test_release_notes.py "
    "-q -p no:cacheprovider"
)
MAX_BULLETS = 6
RECOMMENDED_MIN_BULLETS = 3
MAX_BULLET_CHARS = 110  # clover_cli.release_notes.MAX_BULLET_CHARS

EXIT_REFUSED = 2
EXIT_TEST_FAILED = 3

_STRICT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_TAG_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)$")
_HEADER = re.compile(r"^##\s+(?P<version>[^|\s]+)\s*\|\s*(?P<name>[^|]*?)\s*\|\s*(?P<date>.*?)\s*$")
_DRAFT = re.compile(r"^\s*<!--\s*draft\s*-->\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>\S.*?)\s*$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# `(?=\r?$)` so CRLF checkouts (Windows autocrlf) match too; the \r is never consumed or rewritten.
_INIT_VERSION = re.compile(r'^__version__ = "(?P<v>[^"]+)"(?=\r?$)', re.MULTILINE)
_INIT_DATE = re.compile(r'^__release_date__ = "(?P<d>[^"]+)"(?=\r?$)', re.MULTILINE)


class Refusal(Exception):
    """A precondition failed; nothing was changed."""


class TestFailure(Exception):
    """The release-notes test failed after the bump; edits were rolled back."""

    __test__ = False  # not a pytest class


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------


def parse_strict(version: str) -> tuple[int, int, int]:
    value = version.strip().lstrip("vV")
    if not _STRICT_VERSION.match(value):
        raise Refusal(f"version {version!r} must look like X.Y.Z (digits only, three parts)")
    major, minor, patch = (int(p) for p in value.split("."))
    return major, minor, patch


def _norm(parts: tuple[int, ...]) -> tuple[int, ...]:
    out = list(parts)
    while len(out) > 1 and out[-1] == 0:
        out.pop()
    return tuple(out)


def _loose(version: str) -> Optional[tuple[int, ...]]:
    m = _TAG_VERSION.match(version.strip())
    if not m:
        return None
    return _norm(tuple(int(p) for p in m.group(1).split(".")))


def fmt(version: tuple[int, int, int]) -> str:
    return ".".join(str(p) for p in version)


def bump(current: str, kind: str) -> str:
    major, minor, patch = parse_strict(current)
    if kind == "patch":
        return fmt((major, minor, patch + 1))
    if kind == "minor":
        return fmt((major, minor + 1, 0))
    raise Refusal(f"--bump must be 'patch' or 'minor', got {kind!r} (pass an explicit X.Y.Z for a major)")


def release_date_string(date: dt.date) -> str:
    """``__release_date__`` style: 2026-10-08 -> ``2026.10.8`` (no zero padding)."""
    return f"{date.year}.{date.month}.{date.day}"


# --------------------------------------------------------------------------
# Reading the current state
# --------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def _project_section(text: str) -> tuple[int, int]:
    """Span of the ``[project]`` table in pyproject.toml."""
    start = re.search(r"^\[project\]\s*$", text, re.MULTILINE)
    if not start:
        raise Refusal("pyproject.toml has no [project] table")
    nxt = re.search(r"^\[", text[start.end():], re.MULTILINE)
    return start.end(), start.end() + (nxt.start() if nxt else len(text) - start.end())


def project_name(pyproject_text: str) -> str:
    return tomllib.loads(pyproject_text)["project"]["name"]


def read_versions(root: Path) -> dict[str, str]:
    init = _read(root / INIT_REL)
    pyproject = _read(root / PYPROJECT_REL)
    lock = _read(root / LOCK_REL)
    m = _INIT_VERSION.search(init)
    if not m:
        raise Refusal(f'{INIT_REL}: no line `__version__ = "..."`')
    name = project_name(pyproject)
    lock_matches = _lock_pattern(name).findall(lock)
    if len(lock_matches) != 1:
        raise Refusal(f"{LOCK_REL}: expected exactly one editable [[package]] entry for {name!r}, found {len(lock_matches)}")
    return {
        "init": m.group("v"),
        "pyproject": tomllib.loads(pyproject)["project"]["version"],
        "lock": lock_matches[0][1],
    }


# --------------------------------------------------------------------------
# Edits (each returns new text and asserts it changed exactly one thing)
# --------------------------------------------------------------------------


def edit_init(text: str, new_version: str, new_date: str) -> str:
    out, n = _INIT_VERSION.subn(f'__version__ = "{new_version}"', text, count=1)
    if n != 1:
        raise Refusal(f"{INIT_REL}: __version__ line not found")
    out, n = _INIT_DATE.subn(f'__release_date__ = "{new_date}"', out, count=1)
    if n != 1:
        raise Refusal(f"{INIT_REL}: __release_date__ line not found")
    return out


def edit_pyproject(text: str, new_version: str) -> str:
    lo, hi = _project_section(text)
    body = text[lo:hi]
    new_body, n = re.subn(r'^version = "[^"]+"(?=\r?$)', f'version = "{new_version}"', body, count=1, flags=re.MULTILINE)
    if n != 1:
        raise Refusal("pyproject.toml: [project] version line not found")
    return text[:lo] + new_body + text[hi:]


def _lock_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        r'(\[\[package\]\]\r?\nname = "' + re.escape(name) + r'"\r?\nversion = ")([^"]+)("\r?\nsource = \{ editable = "\." \})'
    )


def edit_lock(text: str, name: str, new_version: str) -> str:
    out, n = _lock_pattern(name).subn(lambda m: m.group(1) + new_version + m.group(3), text)
    if n != 1:
        raise Refusal(f"{LOCK_REL}: expected exactly one editable entry for {name!r}, found {n}")
    return out


# --------------------------------------------------------------------------
# RELEASE_NOTES.md
# --------------------------------------------------------------------------


@dataclass
class Section:
    version: str
    name: str
    date: str
    header_line: int
    end_line: int  # exclusive
    bullets: list[str] = field(default_factory=list)
    draft_lines: list[int] = field(default_factory=list)


def scan_sections(text: str) -> list[Section]:
    """Every ``## v | name | date`` section outside HTML comments, in file order."""
    sections: list[Section] = []
    current: Optional[Section] = None
    in_comment = False
    lines = text.split("\n")
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r")
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if _DRAFT.match(line):
            if current is not None:
                current.draft_lines.append(i)
            continue
        if "<!--" in line and "-->" not in line.split("<!--", 1)[1]:
            in_comment = True
            continue
        if line.startswith("## "):
            if current is not None:
                current.end_line = i
            header = _HEADER.match(line)
            current = None
            if header and _loose(header.group("version")) is not None:
                current = Section(
                    version=header.group("version").lstrip("vV"),
                    name=header.group("name"),
                    date=header.group("date"),
                    header_line=i,
                    end_line=len(lines),
                )
                sections.append(current)
            continue
        if current is not None:
            bullet = _BULLET.match(line)
            if bullet:
                current.bullets.append(bullet.group("text"))
    return sections


@dataclass
class NotesPlan:
    new_text: str
    section: Section
    promoted: bool
    release_date: dt.date
    name: str
    warnings: list[str]

    @property
    def bullets(self) -> list[str]:
        return list(self.section.bullets)


def plan_notes(text: str, version: str, name: Optional[str], date_arg: Optional[dt.date], today: dt.date) -> NotesPlan:
    sections = scan_sections(text)
    key = _loose(version)
    matches = [s for s in sections if _loose(s.version) == key]
    template = f"## {version} | {name or 'Clover C' + version} | YYYY-MM-DD\n- <what a user can now do, or what stopped breaking>\n- ..."
    if not matches:
        raise Refusal(
            f"{NOTES_REL} has no entry for {version}. Write it first (3-6 plain bullets, newest first):\n{template}"
        )
    if len(matches) > 1:
        raise Refusal(f"{NOTES_REL} has {len(matches)} entries for {version}; keep exactly one")
    sec = matches[0]

    seq = [_loose(s.version) or () for s in sections]
    if seq != sorted(seq, reverse=True) or len(set(seq)) != len(seq):
        raise Refusal(f"{NOTES_REL}: entries must be strictly newest-first (found {[s.version for s in sections]})")

    if not sec.bullets:
        raise Refusal(f"{NOTES_REL}: the {version} entry has no '- ' bullets")
    todo = [b for b in sec.bullets if re.search(r"\bTODO\b", b)]
    if todo:
        raise Refusal(f"{NOTES_REL}: the {version} entry still contains TODO: {todo[0]!r}")
    if len(sec.bullets) > MAX_BULLETS:
        raise Refusal(f"{NOTES_REL}: the {version} entry has {len(sec.bullets)} bullets; keep at most {MAX_BULLETS}")
    if not sec.name:
        raise Refusal(f"{NOTES_REL}: the {version} entry has an empty release name")

    warnings: list[str] = []
    if len(sec.bullets) < RECOMMENDED_MIN_BULLETS:
        warnings.append(f"{version} has {len(sec.bullets)} bullet(s); the notes guide recommends {RECOMMENDED_MIN_BULLETS}-{MAX_BULLETS}")
    for b in sec.bullets:
        if len(b) > MAX_BULLET_CHARS:
            warnings.append(f"bullet is {len(b)} chars; users see it cut at {MAX_BULLET_CHARS}: {b[:50]}...")

    wanted = name or sec.name
    if sec.name != wanted:
        raise Refusal(f"{NOTES_REL}: entry name {sec.name!r} != requested name {wanted!r}; fix the entry or the name")

    entry_date: Optional[dt.date] = None
    if _ISO_DATE.match(sec.date):
        try:
            entry_date = dt.date.fromisoformat(sec.date)
        except ValueError:
            raise Refusal(f"{NOTES_REL}: the {version} entry has an impossible date {sec.date!r}") from None
    if entry_date is not None and date_arg is not None and entry_date != date_arg:
        raise Refusal(f"{NOTES_REL}: entry date {entry_date} != --date {date_arg}")
    release_date = date_arg or entry_date or today

    promoted = bool(sec.draft_lines) or entry_date is None
    new_text = text
    if promoted:
        lines = text.split("\n")
        header = lines[sec.header_line]
        parsed = _HEADER.match(header.rstrip("\r"))
        assert parsed is not None
        eol = "\r" if header.endswith("\r") else ""
        lines[sec.header_line] = f"## {sec.version} | {sec.name} | {release_date.isoformat()}{eol}"
        for idx in sorted(sec.draft_lines, reverse=True):
            del lines[idx]
        new_text = "\n".join(lines)
    return NotesPlan(new_text, sec, promoted, release_date, wanted, warnings)


# --------------------------------------------------------------------------
# Git state
# --------------------------------------------------------------------------


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=check
    )


def existing_tag_versions(root: Path, offline: bool, remote: str) -> tuple[list[str], list[str]]:
    """Return (tag names, notes). Local tags always; remote tags unless ``offline``."""
    if _git(root, "rev-parse", "--git-dir", check=False).returncode != 0:
        raise Refusal(f"{root} is not a git checkout, so existing tags cannot be checked")
    tags = _git(root, "tag", "--list").stdout.split()
    notes: list[str] = []
    if offline:
        notes.append("remote tag check skipped (--offline)")
        return tags, notes
    remotes = _git(root, "remote").stdout.split()
    if remote not in remotes:
        notes.append(f"no remote named {remote!r}; checked local tags only")
        return tags, notes
    res = _git(root, "ls-remote", "--tags", "--refs", remote, check=False)
    if res.returncode != 0:
        raise Refusal(f"could not list tags on remote {remote!r} (refusing to guess; use --offline to skip): {res.stderr.strip()}")
    tags += [ln.split("\t", 1)[1].removeprefix("refs/tags/") for ln in res.stdout.splitlines() if "\t" in ln]
    return sorted(set(tags)), notes


def check_dirty(root: Path) -> list[str]:
    status = _git(root, "status", "--porcelain", "--", INIT_REL, PYPROJECT_REL, LOCK_REL, check=False)
    if status.returncode != 0:
        return []
    return [ln for ln in status.stdout.splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass
class Result:
    version: str
    previous: str
    name: str
    date: str
    release_date_string: str
    promoted: bool
    changed_files: list[str]
    bullets: list[str]
    diff: str
    warnings: list[str]
    notes: list[str]
    dry_run: bool


def plan(
    root: Path,
    version_arg: Optional[str],
    bump_kind: Optional[str],
    name: Optional[str],
    date_arg: Optional[dt.date],
    today: dt.date,
    offline: bool,
    remote: str,
) -> tuple[Result, dict[str, str], dict[str, str]]:
    """Validate everything; return (result, old text, new text) of each changed file. Writes nothing."""
    versions = read_versions(root)
    if len(set(versions.values())) != 1:
        raise Refusal(f"version files disagree before the bump: {versions}")
    current = versions["init"]
    cur_t = parse_strict(current)

    if version_arg in ("patch", "minor"):
        if bump_kind and bump_kind != version_arg:
            raise Refusal("give either a version or --bump, not both")
        bump_kind, version_arg = version_arg, None
    if bool(version_arg) == bool(bump_kind):
        raise Refusal("give exactly one of: an X.Y.Z version, or --bump patch|minor")
    new_version = fmt(parse_strict(version_arg)) if version_arg else bump(current, bump_kind or "")
    new_t = parse_strict(new_version)
    if new_t <= cur_t:
        raise Refusal(f"{new_version} is not greater than the current version {current}")

    tags, notes = existing_tag_versions(root, offline, remote)
    tag_versions = [(t, _loose(t)) for t in tags]
    if any(k == _loose(new_version) for _, k in tag_versions):
        raise Refusal(f"tag v{new_version} already exists")
    newest = max((k for _, k in tag_versions if k is not None), default=None)
    if newest is not None and _norm(new_t) <= newest:
        raise Refusal(f"{new_version} is not greater than the newest existing tag ({'.'.join(map(str, newest))})")

    dirty = check_dirty(root)
    if dirty:
        raise Refusal("version files have uncommitted changes; commit or discard them first:\n  " + "\n  ".join(dirty))

    notes_path = root / NOTES_REL
    if not notes_path.is_file():
        raise Refusal(f"{NOTES_REL} not found")
    notes_text = _read(notes_path)
    nplan = plan_notes(notes_text, new_version, name, date_arg, today)
    date_str = release_date_string(nplan.release_date)

    pyproject_text = _read(root / PYPROJECT_REL)
    old = {
        INIT_REL: _read(root / INIT_REL),
        PYPROJECT_REL: pyproject_text,
        LOCK_REL: _read(root / LOCK_REL),
        NOTES_REL: notes_text,
    }
    new = {
        INIT_REL: edit_init(old[INIT_REL], new_version, date_str),
        PYPROJECT_REL: edit_pyproject(pyproject_text, new_version),
        LOCK_REL: edit_lock(old[LOCK_REL], project_name(pyproject_text), new_version),
        NOTES_REL: nplan.new_text,
    }
    changed = [rel for rel in (NOTES_REL, INIT_REL, PYPROJECT_REL, LOCK_REL) if new[rel] != old[rel]]
    diff = "".join(
        line
        for rel in changed
        for line in difflib.unified_diff(
            old[rel].splitlines(keepends=True), new[rel].splitlines(keepends=True), f"a/{rel}", f"b/{rel}", n=3
        )
    )
    warnings = list(nplan.warnings)
    if _git(root, "status", "--porcelain", "--", NOTES_REL, check=False).stdout.strip():
        warnings.append(f"{NOTES_REL} has uncommitted changes; the Release cut workflow only sees what is on main")
    result = Result(
        version=new_version,
        previous=current,
        name=nplan.name,
        date=nplan.release_date.isoformat(),
        release_date_string=date_str,
        promoted=nplan.promoted,
        changed_files=changed,
        bullets=nplan.bullets,
        diff=diff,
        warnings=warnings,
        notes=notes,
        dry_run=False,
    )
    return result, {rel: old[rel] for rel in changed}, {rel: new[rel] for rel in changed}


def run_tests(root: Path, test_cmd: str) -> None:
    proc = subprocess.run(shlex.split(test_cmd), cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
    if proc.returncode != 0:
        raise TestFailure(f"release-notes test failed (exit {proc.returncode}):\n{tail}")
    print("release-notes test: PASS")
    print("  " + (tail.splitlines()[-1] if tail else ""))


def apply(root: Path, result: Result, old: dict[str, str], new: dict[str, str], dry_run: bool, run_test: bool, test_cmd: str) -> None:
    for rel, text in new.items():
        _write(root / rel, text)
    try:
        if run_test:
            run_tests(root, test_cmd)
        else:
            print("release-notes test: skipped (--no-tests)")
        for rel, text in new.items():  # post-write verification
            if _read(root / rel) != text:
                raise Refusal(f"{rel}: content changed unexpectedly after writing")
        after = read_versions(root)
        if set(after.values()) != {result.version}:
            raise Refusal(f"version files disagree after the bump: {after}")
    except BaseException:
        for rel, text in old.items():
            _write(root / rel, text)
        raise
    if dry_run:
        for rel, text in old.items():
            _write(root / rel, text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("version", nargs="?", help="X.Y.Z, or the word patch/minor")
    p.add_argument("--bump", choices=["patch", "minor"], help="compute the next version from the current one")
    p.add_argument("--name", help="release name (default: the RELEASE_NOTES entry's name; 'Clover C<version>' in new entries)")
    p.add_argument("--date", help="release date YYYY-MM-DD (default: the entry's date, else today)")
    p.add_argument("--dry-run", action="store_true", help="validate, show the diff, run the test on the post-bump tree, then restore")
    p.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    p.add_argument("--remote", default="origin")
    p.add_argument("--offline", action="store_true", help="do not ask the remote for existing tags")
    p.add_argument("--no-tests", action="store_true", help="skip the release-notes test")
    p.add_argument("--test-cmd", default=DEFAULT_TEST_CMD, help="command that validates the bumped tree (default: the release-notes test)")
    p.add_argument("--notes-out", help="write the GitHub release body (the entry's bullets) to this file")
    p.add_argument("--summary-json", help="write a machine-readable summary to this file")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.repo_root).resolve()
    try:
        date_arg = dt.date.fromisoformat(args.date) if args.date else None
    except ValueError:
        print(f"refused: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        result, old_text, new_text = plan(root, args.version, args.bump, args.name, date_arg, dt.date.today(), args.offline, args.remote)
        result.dry_run = args.dry_run
        mode = "DRY RUN" if args.dry_run else "RELEASE CUT"
        print(f"{mode}: {result.previous} -> {result.version}  ({result.name}, {result.date})")
        for n in result.notes:
            print(f"note: {n}")
        for w in result.warnings:
            print(f"warning: {w}", file=sys.stderr)
        print(f"files: {', '.join(result.changed_files)}" + ("  (notes entry promoted from draft)" if result.promoted else ""))
        print(result.diff, end="" if result.diff.endswith("\n") else "\n")
        apply(root, result, old_text, new_text, args.dry_run, not args.no_tests, args.test_cmd)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except TestFailure as exc:
        print(f"{exc}\nedits rolled back", file=sys.stderr)
        return EXIT_TEST_FAILED
    if args.notes_out:
        Path(args.notes_out).write_bytes(("\n".join(f"- {b}" for b in result.bullets) + "\n").encode("utf-8"))
    if args.summary_json:
        Path(args.summary_json).write_bytes(
            json.dumps(
                {k: getattr(result, k) for k in ("version", "previous", "name", "date", "release_date_string", "promoted", "changed_files", "bullets", "dry_run")},
                indent=2,
            ).encode("utf-8")
        )
    print("dry run complete: no files were left modified." if args.dry_run else f"done. Next: commit {', '.join(result.changed_files)} on a release/c{result.version} branch (the Release cut workflow does this).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

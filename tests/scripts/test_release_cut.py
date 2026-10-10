"""Behavior contract for scripts/release_cut.py (the local half of the Release cut).

Every test runs against a throwaway git repo built from copies of the real
``clover_cli/__init__.py``, ``pyproject.toml`` and ``uv.lock`` plus synthetic
release notes anchored at the current package version. That prevents a newer
unreleased draft header in the live notes from invalidating tests for the next
release, and nothing in the live checkout is touched or published. The expected
edit shape is the one hand-made in release commit 253629ba (v1.1.2): same four
files, one changed line each (two in __init__.py when the day changes), and
RELEASE_NOTES.md only when a draft is promoted.
"""

from __future__ import annotations

import difflib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release_cut.py"
FILES = ("clover_cli/__init__.py", "pyproject.toml", "uv.lock", "RELEASE_NOTES.md")


def _load():
    spec = importlib.util.spec_from_file_location("release_cut", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["release_cut"] = mod  # dataclasses resolve cls.__module__ here
    spec.loader.exec_module(mod)
    return mod


rc = _load()
PASS_CMD = f"{sys.executable} -c pass"
FAIL_CMD = f"{sys.executable} -c \"import sys; sys.exit(1)\""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout


def _cur(root: Path) -> str:
    return rc.read_versions(root)["init"]


def _next(version: str, kind: str = "patch") -> str:
    return rc.bump(version, kind)


@pytest.fixture()
def repo(tmp_path):
    """A temp git repo holding copies of the four release files + a tag for the current version."""
    root = tmp_path / "repo"
    for rel in FILES:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((REPO_ROOT / rel).read_bytes())
    current = rc.read_versions(root)["init"]
    (root / "RELEASE_NOTES.md").write_text(
        "# Release notes fixture\n\n"
        f"## {current} | Clover C{current} | 2000-01-01\n"
        "- Synthetic previous release.\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "tag", "-a", f"v{current}", "-m", "current")
    return root


def _write_entry(root: Path, version: str, *, name=None, date="2026-10-08", bullets=None, draft=False, todo=False):
    path = root / "RELEASE_NOTES.md"
    text = path.read_text(encoding="utf-8")
    name = name or f"Clover C{version}"
    bullets = ["Fixed: the thing broke.", "You can now do the other thing."] if bullets is None else bullets
    block = f"## {version} | {name} | {date}\n"
    if draft:
        block += "<!-- draft -->\n"
    block += "".join(f"- {b}\n" for b in bullets)
    if todo:
        block += "- TODO(release): fill me in\n"
    block += "\n"
    marker = text.index("\n## ") + 1  # first real section: newest first
    path.write_text(text[:marker] + block + text[marker:], encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "notes")


def _run(root: Path, *args: str, test_cmd=PASS_CMD):
    return rc.main(["--repo-root", str(root), "--offline", "--test-cmd", test_cmd, *args])


def _diff_lines(root: Path, rel: str) -> list[str]:
    old = _git(root, "show", f"HEAD:{rel}").splitlines()
    new = (root / rel).read_text(encoding="utf-8").splitlines()
    return [ln for ln in difflib.unified_diff(old, new, lineterm="", n=0) if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]


# ---------------------------------------------------------------------------
# bumping
# ---------------------------------------------------------------------------


def test_patch_bump_edits_exactly_the_shape_of_a_hand_made_release(repo, capsys):
    cur = _cur(repo)
    new = _next(cur, "patch")
    _write_entry(repo, new)
    assert _run(repo, "--bump", "patch") == 0

    changed = _git(repo, "diff", "--name-only").split()
    assert changed == ["clover_cli/__init__.py", "pyproject.toml", "uv.lock"]  # notes untouched: already final

    init = _diff_lines(repo, "clover_cli/__init__.py")
    assert f'-__version__ = "{cur}"' in init and f'+__version__ = "{new}"' in init
    assert len(init) in (2, 4)  # +2 more only when __release_date__ moved
    if len(init) == 4:
        assert any(ln.startswith("-__release_date__") for ln in init) and any(ln.startswith("+__release_date__") for ln in init)
    assert _diff_lines(repo, "pyproject.toml") == [f'-version = "{cur}"', f'+version = "{new}"']
    assert _diff_lines(repo, "uv.lock") == [f'-version = "{cur}"', f'+version = "{new}"']
    # lock edit lands in the project's own [[package]] entry, not a dependency's
    lock = (repo / "uv.lock").read_text(encoding="utf-8")
    assert re.search(rf'name = "clover-c1"\nversion = "{re.escape(new)}"\nsource = \{{ editable = "\." \}}', lock)
    assert "wrote" not in capsys.readouterr().err


def test_minor_bump_resets_patch(repo):
    cur = _cur(repo)
    major, minor, _ = rc.parse_strict(cur)
    new = f"{major}.{minor + 1}.0"
    _write_entry(repo, new, name=f"Clover C{major}.{minor + 1}")
    assert _run(repo, "minor") == 0
    assert rc.read_versions(repo) == {"init": new, "pyproject": new, "lock": new}


def test_explicit_version_with_custom_name_must_match_the_entry(repo, capsys):
    new = _next(_cur(repo))
    _write_entry(repo, new, name="Clover C Special")
    assert _run(repo, new, "--name", "Something Else") == rc.EXIT_REFUSED
    assert "!= requested name" in capsys.readouterr().err
    assert _run(repo, new, "--name", "Clover C Special") == 0


def test_release_date_is_derived_from_the_entry_without_zero_padding(repo):
    new = _next(_cur(repo))
    _write_entry(repo, new, date="2027-03-04")
    assert _run(repo, new) == 0
    assert '__release_date__ = "2027.3.4"' in (repo / "clover_cli/__init__.py").read_text(encoding="utf-8")


def test_bump_helpers():
    assert rc.bump("1.1.3", "patch") == "1.1.4"
    assert rc.bump("1.1.3", "minor") == "1.2.0"
    assert rc.bump("1.9.9", "patch") == "1.9.10"
    assert rc.release_date_string(__import__("datetime").date(2026, 10, 8)) == "2026.10.8"
    with pytest.raises(rc.Refusal):
        rc.bump("1.1.3", "major")


# ---------------------------------------------------------------------------
# the replay: our edit == the edit the 253629ba release made by hand
# ---------------------------------------------------------------------------


def test_edits_reproduce_the_hand_made_v1_1_2_release_exactly():
    """Apply the pure edit functions to the pre-1.1.2 shapes and compare to 253629ba's result."""
    init_old = 'import os\n__version__ = "1.1.1"\n__release_date__ = "2026.10.7"\n\n\ndef f(): ...\n'
    init_new = 'import os\n__version__ = "1.1.2"\n__release_date__ = "2026.10.7"\n\n\ndef f(): ...\n'
    assert rc.edit_init(init_old, "1.1.2", "2026.10.7") == init_new

    pyproject_old = '[project]\nname = "clover-c1"\nversion = "1.1.1"\ndescription = "x"\n[tool.other]\nversion = "9.9.9"\n'
    assert rc.edit_pyproject(pyproject_old, "1.1.2") == pyproject_old.replace('version = "1.1.1"', 'version = "1.1.2"')  # [tool.other] untouched

    lock_old = (
        '[[package]]\nname = "certifi"\nversion = "1.1.1"\nsource = { registry = "https://pypi.org/simple" }\n\n'
        '[[package]]\nname = "clover-c1"\nversion = "1.1.1"\nsource = { editable = "." }\ndependencies = [\n]\n'
    )
    lock_new = rc.edit_lock(lock_old, "clover-c1", "1.1.2")
    assert lock_new == lock_old.replace(
        'name = "clover-c1"\nversion = "1.1.1"', 'name = "clover-c1"\nversion = "1.1.2"'
    )
    assert lock_new.count('version = "1.1.1"') == 1  # certifi kept its own


# ---------------------------------------------------------------------------
# release notes
# ---------------------------------------------------------------------------


def test_missing_entry_is_refused_with_a_template(repo, capsys):
    assert _run(repo, "--bump", "patch") == rc.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "has no entry for" in err and "| Clover C" in err
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        (dict(bullets=[], draft=True), "no '- ' bullets"),
        (dict(bullets=["TODO(release): replace these placeholder lines."], draft=True), "TODO"),
        (dict(todo=True), "TODO"),
        (dict(bullets=[f"Point {i}." for i in range(7)]), "at most 6"),
    ],
)
def test_unusable_entries_are_refused_and_nothing_changes(repo, capsys, kwargs, needle):
    new = _next(_cur(repo))
    _write_entry(repo, new, **kwargs)
    assert _run(repo, new) == rc.EXIT_REFUSED
    assert needle in capsys.readouterr().err
    assert _git(repo, "status", "--porcelain") == ""  # not a single byte written


def test_draft_with_real_bullets_is_promoted_and_only_the_header_and_marker_change(repo):
    cur = _cur(repo)
    new = _next(cur)
    _write_entry(repo, new, date="TBD", draft=True)
    assert _run(repo, new, "--date", "2026-10-09") == 0
    notes = _diff_lines(repo, "RELEASE_NOTES.md")
    assert notes == [
        f"-## {new} | Clover C{new} | TBD",
        "-<!-- draft -->",
        f"+## {new} | Clover C{new} | 2026-10-09",
    ]
    assert sorted(_git(repo, "diff", "--name-only").split()) == sorted(FILES[:3] + ("RELEASE_NOTES.md",))
    assert '__release_date__ = "2026.10.9"' in (repo / "clover_cli/__init__.py").read_text(encoding="utf-8")


def test_draft_promotion_uses_today_when_no_date_is_given(repo):
    new = _next(_cur(repo))
    _write_entry(repo, new, date="TBD", draft=True)
    result, _, new_text = rc.plan(repo, new, None, None, None, __import__("datetime").date(2030, 1, 2), True, "origin")
    assert f"## {new} | Clover C{new} | 2030-01-02" in new_text["RELEASE_NOTES.md"]
    assert result.promoted and result.release_date_string == "2030.1.2"


def test_entries_out_of_order_are_refused(repo, capsys):
    new = _next(_cur(repo))
    _write_entry(repo, new)
    path = repo / "RELEASE_NOTES.md"
    text = path.read_text(encoding="utf-8")
    # move the new entry to the end of the file
    start = text.index(f"## {new} |")
    end = text.index("\n## ", start) + 1
    path.write_text(text[:start] + text[end:] + "\n" + text[start:end], encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "oops")
    assert _run(repo, new) == rc.EXIT_REFUSED
    assert "newest-first" in capsys.readouterr().err


def test_commented_out_example_headers_are_not_entries(repo):
    new = _next(_cur(repo))
    text = (repo / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    assert rc.scan_sections("<!--\n## 9.9.9 | Nope | 2026-01-01\n- x\n-->\n" + text)[0].version != "9.9.9"
    _write_entry(repo, new)
    assert _run(repo, new) == 0


# ---------------------------------------------------------------------------
# version refusals
# ---------------------------------------------------------------------------


def test_existing_local_tag_is_refused(repo, capsys):
    new = _next(_cur(repo))
    _write_entry(repo, new)
    _git(repo, "tag", f"v{new}")
    assert _run(repo, new) == rc.EXIT_REFUSED
    assert f"tag v{new} already exists" in capsys.readouterr().err


def test_existing_remote_tag_is_refused_even_if_not_fetched_locally(repo, tmp_path, capsys):
    new = _next(_cur(repo))
    _write_entry(repo, new)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "tag", f"v{new}")
    _git(repo, "push", "-q", "origin", f"v{new}")
    _git(repo, "tag", "-d", f"v{new}")  # only the remote has it now
    rc_code = rc.main(["--repo-root", str(repo), "--test-cmd", PASS_CMD, new])
    assert rc_code == rc.EXIT_REFUSED
    assert f"tag v{new} already exists" in capsys.readouterr().err


@pytest.mark.parametrize("pick", ["same", "lower"])
def test_version_not_greater_than_current_is_refused(repo, capsys, pick):
    cur = _cur(repo)
    major, minor, patch = rc.parse_strict(cur)
    bad = cur if pick == "same" else f"{major}.{minor}.{max(patch - 1, 0)}" if patch else f"{max(major - 1, 0)}.9.9"
    if bad == cur and pick == "lower":
        pytest.skip("cannot form a lower version here")
    assert _run(repo, bad) == rc.EXIT_REFUSED
    assert "not greater than the current version" in capsys.readouterr().err


def test_version_below_the_newest_tag_is_refused(repo, capsys):
    cur = _cur(repo)
    major, minor, patch = rc.parse_strict(cur)
    ahead = f"{major}.{minor}.{patch + 5}"
    _git(repo, "tag", f"v{ahead}")  # a tag ahead of the files, e.g. a half-finished release
    target = _next(cur)
    _write_entry(repo, target)
    assert _run(repo, target) == rc.EXIT_REFUSED
    assert "not greater than the newest existing tag" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["1.1", "1.1.x", "v", "1.1.4-rc1", "banana"])
def test_malformed_versions_are_refused(repo, bad):
    assert _run(repo, bad) == rc.EXIT_REFUSED


def test_version_and_bump_are_mutually_exclusive(repo):
    assert _run(repo) == rc.EXIT_REFUSED
    assert _run(repo, "1.9.9", "--bump", "patch") == rc.EXIT_REFUSED


def test_version_files_that_already_disagree_are_refused(repo, capsys):
    new = _next(_cur(repo))
    _write_entry(repo, new)
    p = repo / "pyproject.toml"
    p.write_text(p.read_text(encoding="utf-8").replace(f'version = "{_cur(repo)}"', 'version = "0.0.1"', 1), encoding="utf-8")
    _git(repo, "commit", "-qam", "drift")
    assert _run(repo, new) == rc.EXIT_REFUSED
    assert "disagree" in capsys.readouterr().err


def test_uncommitted_edits_to_version_files_are_refused(repo, capsys):
    new = _next(_cur(repo))
    _write_entry(repo, new)
    p = repo / "pyproject.toml"
    p.write_text(p.read_text(encoding="utf-8") + "\n# local\n", encoding="utf-8")
    assert _run(repo, new) == rc.EXIT_REFUSED
    assert "uncommitted" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# dry run, tests, outputs
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_diff_runs_the_test_on_the_bumped_tree_and_leaves_no_trace(repo, capsys):
    new = _next(_cur(repo))
    _write_entry(repo, new)
    probe = f"{sys.executable} -c \"import pathlib,sys; sys.exit(0 if '{new}' in pathlib.Path('pyproject.toml').read_text() else 7)\""
    assert _run(repo, new, "--dry-run", test_cmd=probe) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and f'+__version__ = "{new}"' in out and "dry run complete" in out
    assert _git(repo, "status", "--porcelain") == ""  # test saw the bump (exit 0) AND everything was restored


def test_failing_release_notes_test_rolls_everything_back(repo, capsys):
    new = _next(_cur(repo))
    _write_entry(repo, new)
    assert _run(repo, new, test_cmd=FAIL_CMD) == rc.EXIT_TEST_FAILED
    assert "rolled back" in capsys.readouterr().err
    assert _git(repo, "status", "--porcelain") == ""


def test_notes_and_summary_outputs_are_the_entry_bullets(repo, tmp_path):
    new = _next(_cur(repo))
    _write_entry(repo, new, bullets=["First thing.", "Second thing.", "Third thing."])
    notes, summary = tmp_path / "n.md", tmp_path / "s.json"
    assert _run(repo, new, "--notes-out", str(notes), "--summary-json", str(summary)) == 0
    assert notes.read_text(encoding="utf-8") == "- First thing.\n- Second thing.\n- Third thing.\n"
    import json

    data = json.loads(summary.read_text(encoding="utf-8"))
    assert data["version"] == new and data["name"] == f"Clover C{new}" and data["bullets"][0] == "First thing."
    assert data["changed_files"] == ["clover_cli/__init__.py", "pyproject.toml", "uv.lock"]


def test_crlf_line_endings_survive_the_edit(repo):
    new = _next(_cur(repo))
    _write_entry(repo, new)
    p = repo / "pyproject.toml"
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    _git(repo, "commit", "-qam", "crlf")
    assert _run(repo, new) == 0
    data = p.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") and f'version = "{new}"'.encode() in data


def test_the_real_release_notes_test_is_the_default_validator():
    assert "tests/clover_cli/test_release_notes.py" in rc.DEFAULT_TEST_CMD

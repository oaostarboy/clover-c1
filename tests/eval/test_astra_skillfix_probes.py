"""Isolation-harness probes from the SKILLFIX review, flipped to pin the fix.

Synthetic profiles only: no credentials, no network, no model.

* A link inside the profile (a skill reference linked from elsewhere, one that
  escapes the profile, a directory link, a cycle) must never let a rep write
  through to the real profile, the frozen baseline, or another rep.
* A config.yaml the harness cannot use is reported by exception type and
  line/column only; its contents (it can hold secrets) reach neither the
  progress output, the run's error, nor results.json.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eval" / "delegation_modes.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("delegation_modes_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["delegation_modes_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


dm = _load_module()

pytestmark = pytest.mark.skipif(
    not hasattr(Path, "symlink_to"), reason="no symlink support")


def _link(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"cannot create symlinks here: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# links never reach the profile, the baseline or another rep
# ---------------------------------------------------------------------------


def test_a_linked_skill_file_is_copied_not_shared(tmp_path):
    source = tmp_path / "source"
    skill = source / "skills" / "demo"
    skill.mkdir(parents=True)
    original = skill / "SKILL.md"
    original.write_text("original")
    _link(skill / "reference.md", original)  # a linked reference, as a skill library has

    baseline = dm.FrozenBaseline.freeze(source, tmp_path / "eval" / "baseline")
    rep1 = baseline.make_home("one")
    rep2 = baseline.make_home("two")

    for home in (baseline.root, rep1, rep2):
        ref = home / "skills" / "demo" / "reference.md"
        assert ref.read_text() == "original"
        assert not ref.is_symlink()

    (rep1 / "skills/demo/reference.md").write_text("changed by rep one")
    assert original.read_text() == "original"
    assert (rep2 / "skills/demo/reference.md").read_text() == "original"
    assert (baseline.root / "skills/demo/reference.md").read_text() == "original"
    assert baseline.skipped_links == []


def test_links_that_leave_the_profile_are_copied_in_not_followed_out(tmp_path):
    outside = tmp_path / "shared-library"
    (outside / "refs").mkdir(parents=True)
    (outside / "refs" / "games.md").write_text("shared notes")
    (outside / "SKILL.md").write_text("---\nname: shared\n---\nbody\n")

    source = tmp_path / "source"
    (source / "skills" / "cat").mkdir(parents=True)
    _link(source / "skills" / "cat" / "shared", outside, directory=True)
    _link(source / "memories", outside / "refs", directory=True)  # a linked top-level dir

    baseline = dm.FrozenBaseline.freeze(source, tmp_path / "eval" / "baseline")
    home = baseline.make_home("one")
    assert (home / "skills/cat/shared/refs/games.md").read_text() == "shared notes"
    assert (home / "memories/games.md").read_text() == "shared notes"
    assert not any(p.is_symlink() for p in home.rglob("*"))
    # the linked skill is hashed like any other
    assert set(baseline.skill_sha256) == {"skills/cat/shared/SKILL.md"}

    (home / "skills/cat/shared/refs/games.md").write_text("rep wrote this")
    (home / "memories/games.md").write_text("rep wrote this too")
    assert (outside / "refs" / "games.md").read_text() == "shared notes"
    assert (baseline.root / "skills/cat/shared/refs/games.md").read_text() == "shared notes"


def test_a_dangling_link_is_skipped_and_recorded_not_fatal(tmp_path):
    source = tmp_path / "source"
    skill = source / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("body")
    _link(skill / "gone.md", tmp_path / "does-not-exist.md")
    _link(source / "auth.json", tmp_path / "also-missing.json")

    baseline = dm.FrozenBaseline.freeze(source, tmp_path / "eval" / "baseline")
    home = baseline.make_home("one")

    assert not (home / "skills/demo/gone.md").exists()
    assert not (home / "skills/demo/gone.md").is_symlink()
    assert not (home / "auth.json").exists()
    assert (home / "skills/demo/SKILL.md").read_text() == "body"
    recorded = {e["path"]: e["reason"] for e in baseline.skipped_links}
    assert recorded == {
        "skills/demo/gone.md": "dangling link",
        "auth.json": "dangling link",
    }
    # only profile-relative paths: no link target, no content
    assert "does-not-exist" not in json.dumps(baseline.skipped_links)


def test_a_link_cycle_is_skipped_not_followed_forever(tmp_path):
    source = tmp_path / "source"
    (source / "skills" / "demo" / "loop").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("body")
    _link(source / "skills" / "demo" / "loop" / "back", source / "skills" / "demo", directory=True)

    baseline = dm.FrozenBaseline.freeze(source, tmp_path / "eval" / "baseline")
    assert [e["reason"] for e in baseline.skipped_links] == ["link cycle"]
    assert (baseline.root / "skills/demo/SKILL.md").read_text() == "body"


def test_the_real_profile_is_not_written_through_a_link_by_the_runner(tmp_path):
    """Same as the first probe, but through make_rep_env/run_eval end to end."""
    source = tmp_path / "source"
    skill = source / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("original")
    _link(skill / "reference.md", skill / "SKILL.md")
    baseline = dm.FrozenBaseline.freeze(source, tmp_path / "eval" / "baseline")
    homes: list[Path] = []

    class _Driver:
        def session(self, **kw):
            raise RuntimeError("no gateway in a probe")

    def driver_factory(home):
        homes.append(home)
        (home / "skills/demo/reference.md").write_text("learned something")
        return _Driver()

    runs = dm.run_eval(
        [{"id": "probe", "prompt": "hi", "expected_mode": "direct"}], None, reader=None,
        root_model="fake", root_provider=None, reps=2, evidence_reps=1,
        out_dir=tmp_path / "out", turn_timeout=1, settle_timeout=1,
        quiet_seconds=0, poll_seconds=0,
        rep_env=dm.make_rep_env(baseline, driver_factory, lambda home: (lambda sid: [])),
    )
    assert len(homes) == 2 and len(runs) == 2
    assert (skill / "SKILL.md").read_text() == "original"
    assert (baseline.root / "skills/demo/reference.md").read_text() == "original"


# ---------------------------------------------------------------------------
# config contents never reach an error
# ---------------------------------------------------------------------------

MARKER = "SYNTHETIC_SECRET_DO_NOT_USE"


@pytest.mark.parametrize("raw", [
    "api_key: [" + MARKER + "\n",                       # unclosed flow sequence
    "model:\n  default: x\n api_key: " + MARKER + "\n",  # bad indentation
    "key: \"" + MARKER + "\n",                          # unterminated quote
    "a: b: " + MARKER + "\n",                          # nested mapping on one line
])
def test_unusable_config_is_reported_without_its_contents(tmp_path, raw):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text(raw)
    (source / ".env").write_text("OPENROUTER_API_KEY=" + MARKER + "\n")
    baseline = dm.FrozenBaseline.freeze(source, tmp_path / "eval" / "baseline")
    progress: list[str] = []
    cases = [{"id": "probe", "prompt": "hi", "expected_mode": "direct"}]

    runs = dm.run_eval(
        cases, None, reader=None, root_model="fake", root_provider=None,
        reps=1, evidence_reps=1, out_dir=tmp_path / "out", turn_timeout=1,
        settle_timeout=1, quiet_seconds=0, poll_seconds=0,
        rep_env=dm.make_rep_env(baseline, lambda home: None, lambda home: (lambda sid: [])),
        progress=progress.append,
    )
    dm.write_report(tmp_path / "out", runs, cases, {"checks": [], "passed": False}, {})

    error = runs[0]["error"]
    assert error and "config.yaml" in error
    # type and position are enough to find the problem ...
    assert "Error" in error and "line " in error and "column " in error
    # ... and nothing else leaves the file
    assert MARKER not in error
    assert MARKER not in "\n".join(progress)
    assert MARKER not in (tmp_path / "out" / "results.json").read_text()
    assert MARKER not in (tmp_path / "out" / "summary.txt").read_text()
    # no half-made copy (it would hold .env) is left behind
    assert not (tmp_path / "eval" / "homes" / "probe-r0").exists()


def test_the_error_carries_no_chained_parser_exception(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("api_key: [" + MARKER + "\n")
    baseline = dm.FrozenBaseline.freeze(source, tmp_path / "eval" / "baseline")
    with pytest.raises(ValueError) as info:
        baseline.make_home("one")
    assert MARKER not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_an_unreadable_config_is_reported_by_type_only(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_bytes(b"api_key: \xff\xfe" + MARKER.encode() + b"\n")
    baseline = dm.FrozenBaseline.freeze(source, tmp_path / "eval" / "baseline")
    with pytest.raises(ValueError) as info:
        baseline.make_home("one")
    assert "config.yaml" in str(info.value)
    assert "UnicodeDecodeError" in str(info.value)
    assert MARKER not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_main_records_skipped_links_in_results_json(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("body")
    _link(source / "skills" / "demo" / "gone.md", tmp_path / "nowhere.md")
    monkeypatch.setattr(dm, "resolve_profile_home", lambda name: source)

    class _Boom:
        def session(self, **kw):
            raise RuntimeError("no gateway in a probe")

    cases = Path(__file__).resolve().parent / "delegation_modes.jsonl"
    out = tmp_path / "o"
    rc = dm.main(
        ["--profile", "dev", "--root-model", "m", "--out", str(out),
         "--cases", str(cases), "--only", "disk-usage"],
        driver_factory=lambda home, args: _Boom(),
        reader_factory=lambda home: (lambda sid: []),
    )
    capsys.readouterr()
    assert rc == 2
    meta = json.loads((out / "results.json").read_text())["meta"]
    assert meta["skipped_links"] == [
        {"path": "skills/demo/gone.md", "reason": "dangling link"}]

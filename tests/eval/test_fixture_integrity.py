"""Fixture integrity for the live delegation-mode eval.

The ``fix-three-bugs`` and ``feature-flag-review`` shops lost ``reports/export.py``
to an unanchored ``export*`` line in .gitignore: the file existed on the author's
disk, was never committed, and every clean checkout ran the case against a shop
whose prompt named a file that was not there.  These tests pin the contract:
every file a case's prompt names exists in the tree, is not ignored by git, and
is in the workdir before the prompt is sent; a missing one is a harness error on
that rep, never a scored run.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "eval" / "delegation_modes.py"
FIXTURES = REPO_ROOT / "tests" / "eval" / "fixtures"
CASE_FILES = [
    REPO_ROOT / "tests" / "eval" / "delegation_modes.jsonl",
    REPO_ROOT / "tests" / "eval" / "delegation_fleet.jsonl",
]


def _load_module():
    spec = importlib.util.spec_from_file_location("delegation_modes_fixture_integrity", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["delegation_modes_fixture_integrity"] = mod
    spec.loader.exec_module(mod)
    return mod


dm = _load_module()


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def test_every_file_a_case_expects_is_in_its_fixture_tree():
    assert dm.CASE_FIXTURE_FILES, "no case declares fixture files"
    for case_id, rels in dm.CASE_FIXTURE_FILES.items():
        for rel in rels:
            assert (FIXTURES / case_id / rel).is_file(), f"{case_id}: {rel} is not in the fixture tree"


def test_every_declared_case_exists_and_its_prompt_names_the_fixture_root():
    cases = {}
    for path in CASE_FILES:
        cases.update({c["id"]: c for c in dm.load_cases(path)})
    for case_id, rels in dm.CASE_FIXTURE_FILES.items():
        assert case_id in cases, f"{case_id} declares fixtures but is not a case"
        roots = {rel.split("/", 1)[0] for rel in rels}
        for root in roots:
            assert "{workdir}/" + root in cases[case_id]["prompt"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_no_fixture_file_is_swallowed_by_a_gitignore_rule(tmp_path):
    # Judge the repo's real .gitignore rules in a scratch repository, so the
    # answer does not depend on this tree being a work tree or on what is
    # tracked yet: a rule that matches is exactly what drops a file from a
    # clean checkout.
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(REPO_ROOT / ".gitignore", repo / ".gitignore")
    env = {"PATH": __import__("os").environ.get("PATH", ""), "HOME": str(tmp_path),
           "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    paths = [str(p.relative_to(REPO_ROOT)) for p in _fixture_files()]
    paths += [f"tests/eval/fixtures/{cid}/{rel}"
              for cid, rels in dm.CASE_FIXTURE_FILES.items() for rel in rels]
    done = subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", "--stdin"],
        cwd=repo, env=env, input="\n".join(paths), capture_output=True, text=True,
    )
    assert done.returncode == 1, f"ignored fixture file(s):\n{done.stdout}{done.stderr}"


def test_seeding_puts_every_expected_file_in_the_workdir(tmp_path):
    for case_id in dm.CASE_FIXTURE_FILES:
        cwd = tmp_path / case_id
        cwd.mkdir()
        dm.seed_case_fixtures(case_id, cwd)
        assert dm.missing_case_fixtures(case_id, cwd) == []


def test_missing_case_fixtures_names_what_is_absent(tmp_path):
    dm.seed_case_fixtures("fix-three-bugs", tmp_path)
    (tmp_path / "shop" / "reports" / "export.py").unlink()
    assert dm.missing_case_fixtures("fix-three-bugs", tmp_path) == ["shop/reports/export.py"]
    assert dm.missing_case_fixtures("disk-usage", tmp_path) == []  # no fixtures declared


class _NeverStartedDriver:
    """A session means the prompt was sent; the guard must stop the rep first."""

    def __init__(self):
        self.started = 0

    @contextmanager
    def session(self, *, model, provider, cwd):
        self.started += 1
        raise AssertionError("prompt must not be sent into a workdir with missing fixtures")
        yield  # pragma: no cover


def test_a_rep_with_a_missing_fixture_is_an_error_not_a_scored_run(tmp_path, monkeypatch):
    monkeypatch.setitem(dm.CASE_FIXTURE_FILES, "fix-three-bugs",
                        dm.CASE_FIXTURE_FILES["fix-three-bugs"] + ("shop/reports/lost.py",))
    case = {"id": "fix-three-bugs", "prompt": "fix {workdir}/shop", "expected_mode": "parallel",
            "live_evidence": False, "override": False}
    driver = _NeverStartedDriver()
    runs = dm.run_eval(
        [case], driver, reader=lambda sid: [], root_model="m", root_provider=None,
        reps=1, evidence_reps=1, out_dir=tmp_path / "out", turn_timeout=1, settle_timeout=1,
        quiet_seconds=1, poll_seconds=1, clock=lambda: 0.0, sleep=lambda dt: None,
    )

    assert driver.started == 0
    (run,) = runs
    assert run["error"].startswith("FixtureError") and "shop/reports/lost.py" in run["error"]
    assert run["score"] is None and run["correct"] is False
    bar = dm.evaluate_bar(runs, [case], {"min_correct": 1, "of": 1})
    assert bar["passed"] is False
    assert any(c["name"] == "run_errors" and not c["ok"] for c in bar["checks"])


def test_the_default_case_files_still_parse_and_do_not_share_ids():
    ids = []
    for path in CASE_FILES:
        ids += [c["id"] for c in dm.load_cases(path)]
    assert len(ids) == len(set(ids))
    # fleet cases live only in the fleet file; the 15 carry no fleet_check
    modes = dm.load_cases(CASE_FILES[0])
    assert not any(c.get("fleet_check") for c in modes)
    fleet = dm.load_cases(CASE_FILES[1])
    assert fleet and all(c.get("fleet_check") in dm.FLEET_CHECKS for c in fleet)
    json.dumps(fleet)  # plain data

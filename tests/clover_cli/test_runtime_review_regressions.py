"""Counterexamples found during the independent updater source review."""
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest
from clover_cli import managed_uv as m


@pytest.mark.parametrize("version", ["3.53.1", "unknown"])
def test_safe_or_unknown_invoking_runtime_does_not_make_ownership_refusal_fatal(
    tmp_path, monkeypatch, version
):
    monkeypatch.setattr(sqlite3, "sqlite_version", version)
    repair = m.RuntimeRepairResult("refused", "unproved target")
    with (
        patch.object(m, "resolve_uv", return_value=str(tmp_path / "uv")),
        patch.object(m, "_uv_self_update_is_fresh", return_value=True),
        patch.object(m, "repair_vulnerable_runtime", return_value=repair),
        patch.object(m, "probe_sqlite_runtime", side_effect=AssertionError("foreign execution")),
    ):
        assert m.update_managed_uv() == str(tmp_path / "uv")


def test_safe_unrecognized_layout_does_not_print_runtime_failure(tmp_path, capsys):
    from tests.clover_cli.test_managed_uv import _runtime_info

    with (
        patch.object(m, "resolve_uv", return_value=str(tmp_path / "uv")),
        patch.object(m, "_uv_self_update_is_fresh", return_value=True),
        patch.object(m, "repair_vulnerable_runtime", return_value=m.RuntimeRepairResult("not-applicable")),
        patch.object(m, "probe_sqlite_runtime", return_value=_runtime_info(Path("python"), (3, 53, 1))),
    ):
        assert m.update_managed_uv() == str(tmp_path / "uv")
    assert "runtime was not replaced" not in capsys.readouterr().out


def test_shared_runtime_restart_obligation_protects_a_sibling_profile(tmp_path, monkeypatch):
    import clover_state as state
    from clover_cli import update_cmd

    root = tmp_path / "checkout"
    root.mkdir()
    owner = tmp_path / "owner"
    owner.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    monkeypatch.setattr(m, "_PROJECT_ROOT", root)
    monkeypatch.setattr(state, "__file__", str(root / "clover_state.py"))
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: "fixture")
    monkeypatch.setenv("CLOVER_HOME", str(owner))
    assert m._arm_runtime_restart_obligation()
    monkeypatch.setenv("CLOVER_HOME", str(sibling))
    with sqlite3.connect(sibling / "state.db") as conn:
        conn.execute("CREATE TABLE proof(value TEXT)")
        assert state._owned_database_update_pending(conn)
        (owner / "fleet_restart_pending").unlink()
        assert not state._owned_database_update_pending(conn)


@pytest.mark.parametrize("failure", ["exception", "not-applicable"])
def test_bootstrap_repair_failure_cannot_hide_known_unsafe_runtime(tmp_path, failure):
    from types import SimpleNamespace
    from tests.clover_cli.test_managed_uv import _runtime_info

    uv = tmp_path / "uv"
    uv.write_text("fixture")
    observed = []
    with (
        patch.object(m, "resolve_uv", side_effect=[None, None, str(uv)]),
        patch.object(m, "managed_uv_path", return_value=uv),
        patch.object(m, "_install_uv"),
        patch.object(m, "repair_vulnerable_runtime",
                     side_effect=OSError("fixture repair exception") if failure == "exception" else None,
                     return_value=m.RuntimeRepairResult("not-applicable")),
        patch.object(m, "probe_sqlite_runtime", return_value=_runtime_info(Path("python"), (3, 50, 4))),
        patch.object(m.subprocess, "run", return_value=SimpleNamespace(stdout="uv fixture")),
    ):
        with pytest.raises(m.RuntimeRepairIncomplete):
            m.update_managed_uv(repair_observer=observed.append)
    assert len(observed) == 1


@pytest.mark.parametrize("mode", ["persist", "truncate", "off", "memory"])
def test_update_boundary_retains_a_known_non_wal_mode(tmp_path, monkeypatch, mode):
    import clover_state as state

    if state.is_sqlite_wal_reset_vulnerable():
        pytest.skip("positive safe-runtime transition case")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    (home / "fleet_restart_pending").write_text("pending")
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE proof(value TEXT)")
        assert conn.execute(f"PRAGMA journal_mode={mode}").fetchone()[0] == mode
        assert state.apply_wal_with_fallback(conn) == mode
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == mode


def test_failed_repair_reprobe_never_executes_an_unproved_target(tmp_path, monkeypatch):
    import sys
    from tests.clover_cli.test_managed_uv import _runtime_info

    root = tmp_path / "checkout"
    live = root / "venv"
    python = m._venv_python(live)
    python.parent.mkdir(parents=True)
    python.write_text("unowned interpreter")
    monkeypatch.setattr(m, "_PROJECT_ROOT", root)
    monkeypatch.setattr(m, "_running_editable_venv", lambda root: None)
    seen = []

    def probe(target):
        seen.append(target)
        assert target == Path(sys.executable), "unproved target must not execute"
        return _runtime_info(target, (3, 53, 1))

    monkeypatch.setattr(m, "probe_sqlite_runtime", probe)
    result = m._failed_update_runtime_attempt("fixture failure")
    assert seen == [Path(sys.executable)]
    assert result.sqlite_before == "3.53.1"


def test_external_target_stays_selected_after_cutover_changes_metadata_layout(tmp_path, monkeypatch):
    from tests.clover_cli.test_managed_uv import _make_runtime_install, _runtime_info

    root, local, sentinel = _make_runtime_install(tmp_path)
    live = tmp_path / "external"
    local.rename(live)
    candidate = root / ".clover-runtime" / "candidate"
    candidate.mkdir(parents=True)
    generation = m.managed_python_install_dir(root) / "generation"
    generation.mkdir(parents=True)
    before = _runtime_info(m._venv_python(live), (3, 50, 4))
    after = _runtime_info(Path("supported-python"), (3, 53, 4))
    metadata_visible = True

    def cutover(*args, **kwargs):
        nonlocal metadata_visible
        # A minor upgrade relocates site-packages. The old process's
        # sysconfig still names the old minor and cannot re-read PEP610.
        metadata_visible = False
        return True, None, after, ""

    monkeypatch.setattr(m, "_running_editable_venv", lambda checkout: live if metadata_visible else None)
    monkeypatch.setattr(m, "probe_sqlite_runtime", lambda python: before)
    monkeypatch.setattr(m, "_install_safe_python_generation", lambda *a, **kw: (generation, after.executable, after))
    monkeypatch.setattr(m, "_stage_candidate_venv", lambda *a, **kw: candidate)
    monkeypatch.setattr(m, "_cut_over_candidate", cutover)
    assert m.repair_vulnerable_runtime("uv", project_root=root).repaired
    assert m._default_live_venv(root) == live
    monkeypatch.setattr(m, "probe_sqlite_runtime", lambda python: after)
    assert m.repair_vulnerable_runtime("uv", project_root=root).status == "safe"

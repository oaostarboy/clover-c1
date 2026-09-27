"""`clover doctor` must detect and (with --fix) repair a missing/deleted
bundled skills/ directory.

Before this fix, only ``clover chat`` reseeded an unseeded skills/ dir; a
gateway-only install had no equivalent, and `doctor` had no check for it
at all.
"""

from __future__ import annotations

import contextlib
import io
import sys
import types
from argparse import Namespace

from clover_cli import doctor as doctor_mod


def _doctor_env(monkeypatch, tmp_path):
    home = tmp_path / ".clover"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setattr(doctor_mod, "CLOVER_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from clover_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_clover_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_xai_oauth_auth_status", lambda: {})
    except Exception:
        pass

    return home


def test_run_doctor_reports_missing_bundled_skills(monkeypatch, tmp_path):
    home = _doctor_env(monkeypatch, tmp_path)
    assert not (home / "skills").exists()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert "Bundled skills directory is empty or missing" in out
    # A plain (non-fix) run must never reseed any bundled skill (other,
    # unrelated doctor/tool checks may incidentally create the empty dir).
    assert next((home / "skills").rglob("SKILL.md"), None) is None


def test_run_doctor_fix_reseeds_bundled_skills(monkeypatch, tmp_path):
    home = _doctor_env(monkeypatch, tmp_path)
    assert not (home / "skills").exists()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=True))
    out = buf.getvalue()

    assert "Reseeded bundled skills" in out
    assert next((home / "skills").rglob("SKILL.md"), None) is not None


def test_run_doctor_leaves_populated_bundled_skills_alone(monkeypatch, tmp_path):
    home = _doctor_env(monkeypatch, tmp_path)
    skill_md = home / "skills" / "my-custom-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: my-custom-skill\n---\nCustom.\n", encoding="utf-8")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=True))
    out = buf.getvalue()

    assert "Bundled skills directory populated" in out
    assert "Reseeded bundled skills" not in out
    assert skill_md.exists()

"""`clover doctor` must probe that core runtime dependencies actually import,
and point at the same dependency-sync command the updater uses when one is
missing (not a bare `pip install <one-package>`, which would fetch an
unpinned version instead of the combination pyproject.toml pins).

Before this fix, doctor's "Required Packages" section only checked 5 modules
(openai/rich/dotenv/yaml/httpx) and had no probe at all for the rest of the
load-bearing base dependencies.
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


def test_run_doctor_reports_healthy_core_runtime_deps(monkeypatch, tmp_path):
    _doctor_env(monkeypatch, tmp_path)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert "Core Runtime Dependencies" in out
    # These are all real, installed packages in the test venv.
    assert "Pydantic" in out
    assert "cryptography" in out


def test_run_doctor_reports_missing_core_runtime_dep_with_dependency_sync_hint(
    monkeypatch, tmp_path
):
    _doctor_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        doctor_mod,
        "_CORE_RUNTIME_MODULES",
        [("definitely_not_a_real_clover_dependency", "Fake Load-Bearing Package")],
    )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    out = buf.getvalue()

    assert "Fake Load-Bearing Package" in out
    assert "clover update" in out
    assert "resync Python dependencies" in out
    # Must NOT suggest a one-off pip install of a single unpinned package.
    assert "pip install definitely_not_a_real_clover_dependency" not in out

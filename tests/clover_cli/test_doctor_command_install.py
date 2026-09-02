"""Tests for the Command Installation check in clover doctor."""

import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

import clover_cli.doctor as doctor_mod


def _setup_doctor_env(monkeypatch, tmp_path, venv_name="venv"):
    """Create a minimal CLOVER_HOME + PROJECT_ROOT for doctor tests."""
    home = tmp_path / ".clover"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    # Create a fake venv entry point
    venv_bin_dir = project / venv_name / "bin"
    venv_bin_dir.mkdir(parents=True, exist_ok=True)
    clover_bin = venv_bin_dir / "clover"
    clover_bin.write_text("#!/usr/bin/env python\n# entry point\n")
    clover_bin.chmod(0o755)

    monkeypatch.setattr(doctor_mod, "CLOVER_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))

    # The test process itself runs from a venv that has its own ``clover``
    # script, which would otherwise win over the in-repo fixture. Pretend we
    # are not in a venv so these tests exercise the in-tree fallback.
    _pretend_not_in_venv(monkeypatch)

    # Stub model_tools so doctor doesn't fail on import
    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    # Stub auth checks
    try:
        from clover_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_clover_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
    except Exception:
        pass

    # Stub httpx.get to avoid network calls
    try:
        import httpx
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: types.SimpleNamespace(status_code=200))
    except Exception:
        pass

    return home, project, clover_bin


def _pretend_not_in_venv(monkeypatch):
    """Make ``sys.prefix == sys.base_prefix`` so no active venv is detected."""
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)


def _run_doctor(fix=False):
    """Run doctor and capture stdout."""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=fix))
    return buf.getvalue()


class TestDoctorCommandInstallation:
    """Tests for the ◆ Command Installation section."""





    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_fix_repairs_wrong_symlink(self, monkeypatch, tmp_path):
        home, project, clover_bin = _setup_doctor_env(monkeypatch, tmp_path)

        # Create a symlink pointing to wrong target
        cmd_link_dir = tmp_path / ".local" / "bin"
        cmd_link_dir.mkdir(parents=True)
        cmd_link = cmd_link_dir / "clover"
        wrong_target = tmp_path / "wrong_clover"
        wrong_target.write_text("#!/usr/bin/env python\n")
        cmd_link.symlink_to(wrong_target)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=True)
        assert "Fixed symlink" in out

        # Verify the symlink now points to the correct target
        assert cmd_link.is_symlink()
        assert cmd_link.resolve() == clover_bin.resolve()

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_missing_venv_entry_point_shows_warn(self, monkeypatch, tmp_path):
        home = tmp_path / ".clover"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")

        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        # Do NOT create any venv entry point

        monkeypatch.setattr(doctor_mod, "CLOVER_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _pretend_not_in_venv(monkeypatch)

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)
        try:
            from clover_cli import auth as _auth_mod
            monkeypatch.setattr(_auth_mod, "get_clover_auth_status", lambda: {})
            monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
        except Exception:
            pass
        try:
            import httpx
            monkeypatch.setattr(httpx, "get", lambda *a, **kw: types.SimpleNamespace(status_code=200))
        except Exception:
            pass

        out = _run_doctor(fix=False)
        assert "Command Installation" in out
        assert "Venv entry point not found" in out



    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_termux_uses_prefix_bin(self, monkeypatch, tmp_path):
        """On Termux, the command link dir is $PREFIX/bin."""
        prefix_dir = tmp_path / "termux_prefix"
        prefix_bin = prefix_dir / "bin"
        prefix_bin.mkdir(parents=True)

        home, project, clover_bin = _setup_doctor_env(monkeypatch, tmp_path)

        monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
        monkeypatch.setenv("PREFIX", str(prefix_dir))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=False)
        assert "Command Installation" in out
        assert "$PREFIX/bin" in out


class TestResolveVenvEntryPoint:
    """An out-of-tree venv is a supported install, not a broken one."""

    def _make_venv(self, root: Path) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "clover"
        script.write_text("#!/usr/bin/env python\n")
        script.chmod(0o755)
        return script

    def test_finds_in_tree_venv(self, monkeypatch, tmp_path):
        project = tmp_path / "project"
        expected = self._make_venv(project / "venv")
        _pretend_not_in_venv(monkeypatch)

        assert doctor_mod._resolve_venv_entry_point(project) == expected

    def test_finds_dot_venv(self, monkeypatch, tmp_path):
        project = tmp_path / "project"
        expected = self._make_venv(project / ".venv")
        _pretend_not_in_venv(monkeypatch)

        assert doctor_mod._resolve_venv_entry_point(project) == expected

    def test_finds_out_of_tree_venv_from_sys_prefix(self, monkeypatch, tmp_path):
        """The regression: venv lives outside the repo, install is still valid."""
        project = tmp_path / "project"
        project.mkdir()
        expected = self._make_venv(tmp_path / "elsewhere" / "venv")

        monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere" / "venv"))
        monkeypatch.setattr(sys, "base_prefix", "/usr")

        assert doctor_mod._resolve_venv_entry_point(project) == expected

    def test_active_venv_wins_over_in_tree(self, monkeypatch, tmp_path):
        """When both exist, the venv we are actually running from is the truth."""
        project = tmp_path / "project"
        self._make_venv(project / "venv")
        active = self._make_venv(tmp_path / "elsewhere" / "venv")

        monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere" / "venv"))
        monkeypatch.setattr(sys, "base_prefix", "/usr")

        assert doctor_mod._resolve_venv_entry_point(project) == active

    def test_returns_none_when_nothing_found(self, monkeypatch, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        _pretend_not_in_venv(monkeypatch)

        assert doctor_mod._resolve_venv_entry_point(project) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_out_of_tree_venv_reports_ok_not_reinstall(self, monkeypatch, tmp_path):
        """End to end: doctor must not demand a reinstall for a valid install."""
        home = tmp_path / ".clover"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")

        project = tmp_path / "project"
        project.mkdir(exist_ok=True)  # deliberately NO venv inside the repo
        venv_script = self._make_venv(tmp_path / "elsewhere" / "venv")

        monkeypatch.setattr(doctor_mod, "CLOVER_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere" / "venv"))
        monkeypatch.setattr(sys, "base_prefix", "/usr")

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)
        try:
            import httpx
            monkeypatch.setattr(httpx, "get", lambda *a, **kw: types.SimpleNamespace(status_code=200))
        except Exception:
            pass

        out = _run_doctor(fix=False)
        assert "Venv entry point not found" not in out
        assert "Reinstall entry point" not in out
        # The absolute path is shown because it cannot be made repo-relative.
        assert str(venv_script) in out


"""Regression tests for _apply_profile_override CLOVER_HOME guard (issue #22502).

When CLOVER_HOME is set to the clover root (e.g. systemd hardcodes
CLOVER_HOME=/root/.clover), _apply_profile_override must still read
active_profile and update CLOVER_HOME to the profile directory.

When CLOVER_HOME is already a profile directory (.../profiles/<name>),
_apply_profile_override must trust it and return without re-reading
active_profile (child-process inheritance contract).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace


def _run_apply_profile_override(
    tmp_path, monkeypatch, *, clover_home: str | None, active_profile: str | None,
    argv: list[str] | None = None,
):
    """Run _apply_profile_override in isolation.

    Returns the value of os.environ["CLOVER_HOME"] after the call,
    or None if unset.
    """
    clover_root = tmp_path / ".clover"
    clover_root.mkdir(parents=True, exist_ok=True)

    if active_profile is not None:
        (clover_root / "active_profile").write_text(active_profile)

    if active_profile and active_profile != "default":
        (clover_root / "profiles" / active_profile).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if clover_home is not None:
        monkeypatch.setenv("CLOVER_HOME", clover_home)
    else:
        monkeypatch.delenv("CLOVER_HOME", raising=False)

    monkeypatch.setattr(sys, "argv", argv or ["clover", "gateway", "start"])

    from clover_cli.main import _apply_profile_override
    _apply_profile_override()

    return os.environ.get("CLOVER_HOME")


class TestApplyProfileOverrideCloverHomeGuard:
    """Regression guard for issue #22502.

    Verifies that CLOVER_HOME pointing to the clover root does NOT suppress
    the active_profile check, while CLOVER_HOME already pointing to a
    profile directory IS trusted as-is.
    """

    def test_clover_home_at_root_with_active_profile_is_redirected(
        self, tmp_path, monkeypatch
    ):
        """CLOVER_HOME=/root/.clover + active_profile=coder must redirect
        CLOVER_HOME to .../profiles/coder.

        Bug scenario from #22502: systemd sets CLOVER_HOME to the clover root
        and the user switches to a profile via `clover profile use`.
        Before the fix, the guard returned early and active_profile was ignored.
        """
        clover_root = tmp_path / ".clover"
        clover_root.mkdir(parents=True, exist_ok=True)

        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            clover_home=str(clover_root),
            active_profile="coder",
        )

        assert result is not None, "CLOVER_HOME must be set after profile redirect"
        assert "profiles" in result, (
            f"Expected CLOVER_HOME to point into profiles/ dir, got: {result!r}"
        )
        assert result.endswith("coder"), (
            f"Expected CLOVER_HOME to end with 'coder', got: {result!r}"
        )


    def test_sudo_explicit_profile_resolves_invoking_users_profile(self, tmp_path, monkeypatch):
        """sudo elias ... should resolve `-p elias` under SUDO_USER, not root."""
        root_home = tmp_path / "root"
        user_home = tmp_path / "home" / "clover"
        profile_dir = user_home / ".clover" / "profiles" / "elias"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (root_home / ".clover").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: root_home)
        monkeypatch.setenv("SUDO_USER", "clover")
        monkeypatch.delenv("CLOVER_HOME", raising=False)
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(sys, "argv", ["clover", "-p", "elias", "gateway", "install", "--system"])

        import pwd

        monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=str(user_home)))

        from clover_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("CLOVER_HOME") == str(profile_dir)
        assert sys.argv == ["clover", "gateway", "install", "--system"]




class TestSupervisedChildIgnoresStickyProfile:
    """The reserved default gateway s6 slot must not follow active_profile.

    Inside the Docker s6 image the ``gateway-default`` service slot runs a
    bare ``clover gateway run`` (no ``-p``) to mean "the root CLOVER_HOME
    profile". The run-script exports ``CLOVER_S6_SUPERVISED_CHILD=1``.
    Without a guard, ``_apply_profile_override`` would read the sticky
    ``active_profile`` file (set by e.g. the dashboard profile switcher) and
    redirect the reserved default gateway into that profile — producing a
    duplicate gateway for the active profile and no real default gateway.
    """


    def test_non_supervised_run_still_follows_active_profile(
        self, tmp_path, monkeypatch
    ):
        """Without the sentinel, a normal `clover gateway run` still honors
        active_profile — the guard is scoped strictly to supervised children."""
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            clover_home=None,
            active_profile="briefer",
            argv=["clover", "gateway", "run"],
        )

        assert result is not None
        assert result.endswith("briefer")

    def test_supervised_named_profile_flag_still_wins(self, tmp_path, monkeypatch):
        """A supervised named-profile slot passes ``-p <name>`` explicitly;
        that must still resolve (the sentinel guard only skips the sticky
        active_profile fallback, never an explicit flag)."""
        clover_root = tmp_path / ".clover"
        clover_root.mkdir(parents=True, exist_ok=True)
        (clover_root / "active_profile").write_text("briefer")
        (clover_root / "profiles" / "briefer").mkdir(parents=True, exist_ok=True)
        (clover_root / "profiles" / "coder").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("CLOVER_HOME", raising=False)
        monkeypatch.setenv("CLOVER_S6_SUPERVISED_CHILD", "1")
        monkeypatch.setattr(sys, "argv", ["clover", "-p", "coder", "gateway", "run"])

        from clover_cli.main import _apply_profile_override
        _apply_profile_override()

        result = os.environ.get("CLOVER_HOME")
        assert result is not None
        assert result.endswith("coder")


"""Doctor must not demand `clover setup` from an OAuth-authenticated install.

`.env` is only one of the places a provider can be configured. OAuth logins and
keys added via `clover auth add` land in `auth.json` (`providers` /
`credential_pool`). Before this, an install whose only credentials lived in
`auth.json` was reported as "No API key found" and told to run `clover setup` —
a false positive on a fully working install.
"""

import json
import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

import clover_cli.doctor as doctor_mod


def _write_auth(home: Path, payload: dict) -> None:
    (home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def doctor_home(monkeypatch, tmp_path):
    """A CLOVER_HOME with no credentials anywhere yet."""
    home = tmp_path / ".clover"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(doctor_mod, "CLOVER_HOME", home)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    return home


class TestHasStoredProviderCredentials:
    def test_false_when_auth_json_missing(self, doctor_home, monkeypatch):
        monkeypatch.setitem(sys.modules, "clover_cli.auth", types.SimpleNamespace(
            read_credential_pool=lambda _=None: {}
        ))
        assert doctor_mod._has_stored_provider_credentials() is False

    def test_false_when_pool_and_providers_empty(self, doctor_home, monkeypatch):
        _write_auth(doctor_home, {"providers": [], "credential_pool": {}})
        monkeypatch.setitem(sys.modules, "clover_cli.auth", types.SimpleNamespace(
            read_credential_pool=lambda _=None: {}
        ))
        assert doctor_mod._has_stored_provider_credentials() is False

    def test_false_when_provider_key_has_no_entries(self, doctor_home, monkeypatch):
        """An empty list under a provider name is not a credential."""
        _write_auth(doctor_home, {"providers": [], "credential_pool": {"anthropic": []}})
        monkeypatch.setitem(sys.modules, "clover_cli.auth", types.SimpleNamespace(
            read_credential_pool=lambda _=None: {"anthropic": []}
        ))
        assert doctor_mod._has_stored_provider_credentials() is False

    def test_true_from_credential_pool(self, doctor_home, monkeypatch):
        monkeypatch.setitem(sys.modules, "clover_cli.auth", types.SimpleNamespace(
            read_credential_pool=lambda _=None: {"anthropic": [{"id": "oauth-1"}]}
        ))
        assert doctor_mod._has_stored_provider_credentials() is True

    def test_true_from_providers_dict_when_pool_reader_fails(self, doctor_home, monkeypatch):
        """A broken/absent pool reader must fall back to reading auth.json."""
        _write_auth(doctor_home, {"providers": {"anthropic": {"token": "x"}}})

        def _boom(_=None):
            raise RuntimeError("pool unavailable")

        monkeypatch.setitem(sys.modules, "clover_cli.auth", types.SimpleNamespace(
            read_credential_pool=_boom
        ))
        assert doctor_mod._has_stored_provider_credentials() is True

    def test_survives_corrupt_auth_json(self, doctor_home, monkeypatch):
        """Malformed JSON reports 'no credentials', it does not crash doctor."""
        (doctor_home / "auth.json").write_text("{ not json", encoding="utf-8")

        def _boom(_=None):
            raise RuntimeError("pool unavailable")

        monkeypatch.setitem(sys.modules, "clover_cli.auth", types.SimpleNamespace(
            read_credential_pool=_boom
        ))
        assert doctor_mod._has_stored_provider_credentials() is False


def _run_doctor():
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=False))
    return buf.getvalue()


class TestDoctorConfigurationFilesSection:
    """End-to-end: the Configuration Files section reflects auth.json."""

    def _prep(self, monkeypatch, tmp_path):
        home = tmp_path / ".clover"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "CLOVER_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)
        try:
            import httpx
            monkeypatch.setattr(
                httpx, "get", lambda *a, **kw: types.SimpleNamespace(status_code=200)
            )
        except Exception:
            pass
        return home

    def test_oauth_only_install_is_not_told_to_run_setup(self, monkeypatch, tmp_path):
        home = self._prep(monkeypatch, tmp_path)
        # An .env with no provider keys in it — the OAuth-only shape.
        (home / ".env").write_text("SOME_UNRELATED=1\n", encoding="utf-8")
        monkeypatch.setattr(
            doctor_mod, "_has_stored_provider_credentials", lambda: True
        )

        out = _run_doctor()
        assert "Provider credentials configured" in out
        assert "No API key found" not in out

    def test_truly_unconfigured_install_still_warns(self, monkeypatch, tmp_path):
        home = self._prep(monkeypatch, tmp_path)
        (home / ".env").write_text("SOME_UNRELATED=1\n", encoding="utf-8")
        monkeypatch.setattr(
            doctor_mod, "_has_stored_provider_credentials", lambda: False
        )

        out = _run_doctor()
        assert "No API key found" in out

    def test_missing_env_file_is_ok_when_oauth_present(self, monkeypatch, tmp_path):
        """No .env at all is a legitimate OAuth install, not a failure."""
        self._prep(monkeypatch, tmp_path)  # deliberately no .env written
        monkeypatch.setattr(
            doctor_mod, "_has_stored_provider_credentials", lambda: True
        )

        out = _run_doctor()
        assert "Provider credentials configured" in out
        assert ".env file missing" not in out

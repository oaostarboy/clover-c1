"""Tests for clover_cli.model_catalog — remote manifest fetch + cache + fallback."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolate CLOVER_HOME + reset any module-level catalog cache per test."""
    home = tmp_path / ".clover"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLOVER_HOME", str(home))

    # Force a fresh catalog module state for each test.
    import importlib
    from clover_cli import model_catalog
    importlib.reload(model_catalog)
    yield home
    model_catalog.reset_cache()


def _valid_manifest() -> dict:
    return {
        "version": 1,
        "updated_at": "2026-04-25T22:00:00Z",
        "metadata": {"source": "test"},
        "providers": {
            "openrouter": {
                "metadata": {"display_name": "OpenRouter"},
                "models": [
                    {"id": "anthropic/claude-opus-4.7", "description": "recommended"},
                    {"id": "openai/gpt-5.4", "description": ""},
                    {"id": "openrouter/elephant-alpha", "description": "free"},
                ],
            },
            "clover": {
                "metadata": {"display_name": "Clover Portal"},
                "models": [
                    {"id": "anthropic/claude-opus-4.7"},
                    {"id": "moonshotai/kimi-k2.6"},
                ],
            },
        },
    }


class TestValidation:
    def test_accepts_well_formed_manifest(self, isolated_home):
        from clover_cli.model_catalog import _validate_manifest
        assert _validate_manifest(_valid_manifest()) is True

    def test_rejects_non_dict(self, isolated_home):
        from clover_cli.model_catalog import _validate_manifest
        assert _validate_manifest("string") is False
        assert _validate_manifest([]) is False
        assert _validate_manifest(None) is False


    def test_rejects_non_string_model_id(self, isolated_home):
        from clover_cli.model_catalog import _validate_manifest
        m = _valid_manifest()
        m["providers"]["openrouter"]["models"][0] = {"id": 42}
        assert _validate_manifest(m) is False


class TestFetchSuccess:
    def test_fetch_and_cache_writes_disk(self, isolated_home):
        from clover_cli import model_catalog
        manifest = _valid_manifest()
        with patch.object(
            model_catalog, "_fetch_manifest", return_value=manifest
        ) as fetch:
            result = model_catalog.get_catalog(force_refresh=True)

        assert result == manifest
        assert fetch.called

        cache_file = model_catalog._cache_path()
        assert cache_file.exists()
        with open(cache_file) as fh:
            assert json.load(fh) == manifest


class TestFetchFailure:
    def test_network_failure_returns_empty_when_no_cache(self, isolated_home):
        from clover_cli import model_catalog
        with patch.object(model_catalog, "_fetch_manifest", return_value=None):
            result = model_catalog.get_catalog(force_refresh=True)
        assert result == {}

    def test_network_failure_falls_back_to_disk_cache(self, isolated_home):
        from clover_cli import model_catalog
        # Prime disk cache with a fresh copy.
        manifest = _valid_manifest()
        with patch.object(model_catalog, "_fetch_manifest", return_value=manifest):
            model_catalog.get_catalog(force_refresh=True)

        # Now wipe in-process cache and simulate network failure on refetch.
        model_catalog.reset_cache()
        with patch.object(model_catalog, "_fetch_manifest", return_value=None):
            result = model_catalog.get_catalog(force_refresh=True)

        assert result == manifest

    def test_fetch_failure_falls_back_to_stale_cache(self, isolated_home):
        from clover_cli import model_catalog
        manifest = _valid_manifest()
        # Write stale cache directly (mtime in the past).
        cache = model_catalog._cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w") as fh:
            json.dump(manifest, fh)
        old = time.time() - 30 * 24 * 3600  # 30 days ago
        import os as _os
        _os.utime(cache, (old, old))

        with patch.object(model_catalog, "_fetch_manifest", return_value=None):
            result = model_catalog.get_catalog()

        # Stale cache is better than nothing.
        assert result == manifest


class TestFallbackChain:
    """``_fetch_manifest_with_fallback`` fetches the primary URL only.

    It used to also walk a raw-GitHub mirror of the manifest for when the
    docs-site fetch was bot-gated by Vercel, but that mirror lived under
    the docs site's own repo tree, which is no longer shipped — there is
    no working second URL to fall through to anymore.
    """

    PRIMARY = "docs/api/model-catalog.json"

    def test_uses_primary_when_it_succeeds(self, isolated_home):
        from clover_cli import model_catalog
        calls: list[str] = []

        def fake_fetch(url, timeout):
            calls.append(url)
            return _valid_manifest()

        with patch.object(model_catalog, "_fetch_manifest", side_effect=fake_fetch):
            result = model_catalog._fetch_manifest_with_fallback(self.PRIMARY, 5.0)

        assert result is not None
        assert calls == [self.PRIMARY]

    def test_primary_failure_returns_none(self, isolated_home):
        from clover_cli import model_catalog

        with patch.object(model_catalog, "_fetch_manifest", return_value=None):
            result = model_catalog._fetch_manifest_with_fallback(self.PRIMARY, 5.0)

        assert result is None


class TestCuratedAccessors:
    def test_openrouter_returns_tuples(self, isolated_home):
        from clover_cli import model_catalog
        with patch.object(
            model_catalog, "_fetch_manifest", return_value=_valid_manifest()
        ):
            result = model_catalog.get_curated_openrouter_models()
        assert result == [
            ("anthropic/claude-opus-4.7", "recommended"),
            ("openai/gpt-5.4", ""),
            ("openrouter/elephant-alpha", "free"),
        ]


class TestDefaultModelFromCache:
    """get_default_model_from_cache reads the '"default": true' label without
    ever hitting the network."""

    def _manifest_with_default(self) -> dict:
        m = _valid_manifest()
        m["providers"]["openrouter"]["models"][1]["default"] = True  # gpt-5.4
        m["providers"]["clover"]["models"][1]["default"] = True  # kimi-k2.6
        return m

    def test_reads_label_from_disk_cache(self, isolated_home):
        from clover_cli import model_catalog
        cache = isolated_home / "cache"
        cache.mkdir()
        (cache / "model_catalog.json").write_text(
            json.dumps(self._manifest_with_default())
        )
        with patch.object(model_catalog, "_fetch_manifest") as fetch:
            assert (
                model_catalog.get_default_model_from_cache("openrouter")
                == "openai/gpt-5.4"
            )
            assert (
                model_catalog.get_default_model_from_cache("clover")
                == "moonshotai/kimi-k2.6"
            )
            fetch.assert_not_called()

    def test_no_label_returns_none(self, isolated_home):
        from clover_cli import model_catalog
        cache = isolated_home / "cache"
        cache.mkdir()
        (cache / "model_catalog.json").write_text(json.dumps(_valid_manifest()))
        with patch.object(model_catalog, "_fetch_manifest") as fetch:
            assert model_catalog.get_default_model_from_cache("openrouter") is None
            fetch.assert_not_called()


class TestProviderOverride:
    def test_override_url_takes_precedence(self, isolated_home):
        from clover_cli import model_catalog

        override_payload = {
            "version": 1,
            "providers": {
                "openrouter": {
                    "models": [
                        {"id": "override/model", "description": "custom"},
                    ]
                }
            },
        }

        def fake_fetch(url, timeout):
            if "override" in url:
                return override_payload
            return _valid_manifest()

        with patch.object(
            model_catalog,
            "_load_catalog_config",
            return_value={
                "enabled": True,
                "url": "http://master",
                "ttl_hours": 24.0,
                "providers": {"openrouter": {"url": "http://override"}},
            },
        ):
            with patch.object(model_catalog, "_fetch_manifest", side_effect=fake_fetch):
                result = model_catalog.get_curated_openrouter_models()

        assert result == [("override/model", "custom")]


class TestIntegrationWithModelsModule:
    """Exercise the fallback paths via the real callers in clover_cli.models."""




    def test_picker_max_models_cap_semantics(self, monkeypatch):
        """None is unlimited, zero hides models, one returns one model."""
        from clover_cli.model_switch import list_authenticated_providers, list_picker_providers
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        models = ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"]
        with patch("clover_cli.models.cached_provider_model_ids", side_effect=lambda slug: models if slug == "deepseek" else []), patch("agent.models_dev.fetch_models_dev", return_value={}):
            full = list_picker_providers(current_provider="deepseek", max_models=None)
            one = list_picker_providers(current_provider="deepseek", max_models=1)
            zero = list_authenticated_providers(current_provider="deepseek", max_models=0)

        def row(rows):
            return next((r for r in rows if r["slug"] == "deepseek"), None)

        assert row(full) is not None and row(full)["models"] == models
        assert row(one) is not None and row(one)["models"] == models[:1]
        assert row(zero) is not None and row(zero)["models"] == []
        assert row(zero)["total_models"] == len(models)


# -----------------------------------------------------------------------------
# Drift guard — prevent the in-repo curated lists from going out of sync with
# the docs-hosted manifest at website/static/api/model-catalog.json.
#
# History: qwen/qwen3.6-plus was added to _PROVIDER_MODELS["clover"] in commit
# 9dd6e5510 but website/static/api/model-catalog.json was not regenerated for
# weeks, so free-tier users on a new install fetched a stale manifest and the
# free-tier picker showed "No free models currently available." even though
# the Portal was serving qwen/qwen3.6-plus as free. CI must catch this.
# -----------------------------------------------------------------------------


class TestManifestMatchesInRepoLists:
    """Fail if the on-disk manifest is out of date relative to in-repo lists."""

    @staticmethod
    def _strip_volatile(catalog: dict) -> dict:
        """Drop fields that always change (timestamps) for diff comparison."""
        out = dict(catalog)
        out.pop("updated_at", None)
        return out

    def test_in_repo_lists_match_manifest(self):
        """``scripts/build_model_catalog.py`` output must match the committed file.

        If this fails, run ``python scripts/build_model_catalog.py`` and
        commit the regenerated ``website/static/api/model-catalog.json``.
        """
        # Resolve the repo root from this test file's location.
        repo_root = Path(__file__).resolve().parents[2]
        manifest_path = repo_root / "website" / "static" / "api" / "model-catalog.json"

        if not manifest_path.exists():
            pytest.skip(f"manifest missing at {manifest_path}")

        # Build expected catalog using the same script CI would.
        import importlib.util
        script_path = repo_root / "scripts" / "build_model_catalog.py"
        spec = importlib.util.spec_from_file_location("_build_model_catalog", script_path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        expected = mod.build_catalog()

        with open(manifest_path, encoding="utf-8") as fh:
            actual = json.load(fh)

        assert self._strip_volatile(actual) == self._strip_volatile(expected), (
            "website/static/api/model-catalog.json is out of sync with "
            "_PROVIDER_MODELS['clover'] / OPENROUTER_MODELS. "
            "Run: python scripts/build_model_catalog.py && "
            "git add website/static/api/model-catalog.json"
        )

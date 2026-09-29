"""Codex ``-900k`` picker variants must be strippable on the wire.

The picker and the wire strip share ONE eligibility predicate
(``is_codex_900k_base``); a variant the picker offers but the wire does not
strip is sent literally to Codex and 400s ("unknown provider for model").
"""

import json

import pytest

import agent.model_metadata as mm
from agent.model_metadata import (
    CODEX_CONTEXT_VARIANT_SUFFIX,
    is_codex_context_variant,
    strip_codex_context_variant_suffix,
)


@pytest.fixture(autouse=True)
def _isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(mm, "_codex_live_max_ctx", {})
    monkeypatch.setattr(mm, "_codex_local_cache_state", ("", -1.0))
    monkeypatch.setattr(mm, "_codex_local_max_ctx", {})


def _write_local_catalog(tmp_path, entries):
    home = tmp_path / "codex"
    home.mkdir(parents=True, exist_ok=True)
    (home / "models_cache.json").write_text(json.dumps({"models": entries}), encoding="utf-8")


@pytest.mark.parametrize("base", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"])
def test_gpt6_variants_strip_offline(base):
    assert strip_codex_context_variant_suffix(base + "-900k") == base
    assert is_codex_context_variant(base + "-900k")


def test_gpt6_astra_variant_gets_extended_context_cap():
    ctx, _src = mm._resolve_codex_oauth_context_length_with_source("gpt-6-astra-900k")
    assert ctx == 872_000
    base_ctx, _ = mm._resolve_codex_oauth_context_length_with_source("gpt-6-astra")
    assert base_ctx == 272_000


def test_catalog_max_context_window_makes_unknown_base_eligible(tmp_path):
    _write_local_catalog(
        tmp_path,
        [
            {"slug": "gpt-99-zenith", "context_window": 272000, "max_context_window": 872000},
            {"slug": "gpt-5.5", "context_window": 272000, "max_context_window": 272000},
        ],
    )
    assert strip_codex_context_variant_suffix("gpt-99-zenith-900k") == "gpt-99-zenith"
    # a genuine 272K enforcer never gets a variant
    assert strip_codex_context_variant_suffix("gpt-5.5-900k") == "gpt-5.5-900k"
    # cap comes from the catalog's real max_context_window
    assert mm._verified_codex_ctx_for_slug("gpt-99-zenith-900k") == 872_000


def test_live_catalog_entries_are_recorded():
    mm.record_codex_catalog_entries(
        [{"slug": "gpt-98-live", "context_window": 272000, "max_context_window": 900000}]
    )
    assert is_codex_context_variant("gpt-98-live-900k")
    assert not is_codex_context_variant("gpt-97-unlisted-900k")


def test_picker_variants_are_subset_of_strippable(tmp_path):
    """Invariant: every -900k id the picker emits is stripped by the wire."""
    from clover_cli.codex_models import _finalize_codex_models

    _write_local_catalog(
        tmp_path,
        [{"slug": "gpt-99-zenith", "context_window": 272000, "max_context_window": 872000}],
    )
    bases = [
        "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.4",
        "gpt-5.5", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-99-zenith", "gpt-unknown",
    ]
    picker = _finalize_codex_models(bases)
    variants = [m for m in picker if m.endswith(CODEX_CONTEXT_VARIANT_SUFFIX)]
    assert "gpt-6-astra-900k" in variants and "gpt-99-zenith-900k" in variants
    for v in variants:
        stripped = strip_codex_context_variant_suffix(v)
        assert stripped != v, f"picker offers {v} but the wire would send it literally"
        assert stripped == v[: -len(CODEX_CONTEXT_VARIANT_SUFFIX)]

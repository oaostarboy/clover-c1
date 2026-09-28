"""Sale UI pricing helpers: gateway pricing.original → discount chrome."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import clover_cli.models as models_mod
from clover_cli.models import (
    compute_sale_discount,
    fetch_models_with_pricing,
)


def test_free_model_gets_flat_100_percent_discount():
    """$0/$0 models always show -100%; was_* pass through when present."""
    assert compute_sale_discount("0", "0", None) == (100, "", "")
    assert compute_sale_discount(
        "0", "0", {"prompt": "0.000002", "completion": "0.00001"}
    ) == (100, "0.000002", "0.00001")
    # "0.0000000000" strings (Clover portal shape) count as free too.
    assert compute_sale_discount("0.0000000000", "0.0000000000", None) == (100, "", "")


def test_paid_model_without_original_shows_no_sale():
    assert compute_sale_discount("0.000002", "0.00001", None) is None






def test_fetch_models_with_pricing_copies_nested_original(monkeypatch):
    models_mod._pricing_cache.clear()
    payload = {
        "data": [
            {
                "id": "anthropic/claude-sonnet-5",
                "pricing": {
                    "prompt": "0.0000016",
                    "completion": "0.000008",
                    "input_cache_read": "0.00000016",
                    "original": {
                        "prompt": "0.000002",
                        "completion": "0.00001",
                        "input_cache_read": "0.0000002",
                    },
                },
            },
            {
                "id": "free/model",
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
    }
    body = json.dumps(payload).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda *a: False

    monkeypatch.setattr(
        models_mod,
        "_urlopen_model_catalog_request",
        lambda req, timeout=8.0: resp,
    )

    # Clover Portal opts in via include_sale_original=True.
    result = fetch_models_with_pricing(
        api_key="sk-test",
        base_url="https://example.test",
        force_refresh=True,
        include_sale_original=True,
    )
    paid = result["anthropic/claude-sonnet-5"]
    assert paid["prompt"] == "0.0000016"
    assert paid["completion"] == "0.000008"
    assert paid["original"] == {
        "prompt": "0.000002",
        "completion": "0.00001",
        "input_cache_read": "0.0000002",
    }
    assert "original" not in result["free/model"]




def test_a_failed_catalog_fetch_is_not_cached_forever(monkeypatch):
    """A blip must not disable live model discovery for the whole process.

    The empty result is cached so a dead endpoint isn't re-dialed on every
    call, but it expires — the processes that read this run for weeks, and
    every caller silently falls back to a curated list meanwhile.
    """
    models_mod._pricing_cache.clear()
    models_mod._pricing_cache_retry_after.clear()

    calls = []

    def _fail(req, timeout=8.0):
        calls.append(req)
        raise OSError("connection refused")

    monkeypatch.setattr(models_mod, "_urlopen_model_catalog_request", _fail)

    assert fetch_models_with_pricing(base_url="https://example.test") == {}
    # Inside the window the failure is cached: no second dial.
    assert fetch_models_with_pricing(base_url="https://example.test") == {}
    assert len(calls) == 1

    now = models_mod.time.monotonic()
    monkeypatch.setattr(
        models_mod.time,
        "monotonic",
        lambda: now + models_mod._FAILED_CATALOG_TTL_SECONDS + 1,
    )
    assert fetch_models_with_pricing(base_url="https://example.test") == {}
    assert len(calls) == 2


def test_a_successful_catalog_fetch_stays_cached(monkeypatch):
    """Only the failures expire; a real catalog is still fetched once."""
    models_mod._pricing_cache.clear()
    models_mod._pricing_cache_retry_after.clear()

    calls = []
    body = json.dumps(
        {"data": [{"id": "a/b", "pricing": {"prompt": "1", "completion": "2"}}]}
    ).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda *a: False

    def _ok(req, timeout=8.0):
        calls.append(req)
        return resp

    monkeypatch.setattr(models_mod, "_urlopen_model_catalog_request", _ok)

    assert "a/b" in fetch_models_with_pricing(base_url="https://example.test")
    now = models_mod.time.monotonic()
    monkeypatch.setattr(
        models_mod.time,
        "monotonic",
        lambda: now + models_mod._FAILED_CATALOG_TTL_SECONDS + 1,
    )
    assert "a/b" in fetch_models_with_pricing(base_url="https://example.test")
    assert len(calls) == 1


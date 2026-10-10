"""Anthropic-messages clients honour ``custom_providers[].extra_headers``.

Adapted from NousResearch/hermes-agent 6f6ed01355 (MIT).

The OpenAI-wire clients apply the per-provider headers; ``build_anthropic_client``
used to skip them, so a relay behind a WAF that rejects the SDK User-Agent kept
403ing in anthropic_messages mode. These tests use the real SDK client and a real
config.yaml under the isolated CLOVER_HOME.
"""
import pytest
import yaml

from agent.anthropic_adapter import build_anthropic_client

_ROUTE = "https://proxy.example.com/v1"
_EXTRA = {"User-Agent": "CloverAgent/1.0", "X-Privacy-Tier": "enterprise"}


@pytest.fixture
def waf_config(tmp_path, monkeypatch):
    home = tmp_path / "clover_home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    (home / "config.yaml").write_text(yaml.safe_dump({"custom_providers": [{
        "name": "wafproxy", "base_url": _ROUTE, "api_mode": "anthropic_messages",
        "extra_headers": _EXTRA,
    }]}))
    return home


def _headers(route, key="sk-test"):
    return build_anthropic_client(key, route).default_headers


def test_matching_route_merges_extra_headers_after_betas(waf_config):
    headers = _headers(_ROUTE)
    assert headers["User-Agent"] == "CloverAgent/1.0"
    assert headers["X-Privacy-Tier"] == "enterprise"
    assert "anthropic-beta" in headers  # provider headers add to, not replace, the betas


def test_other_route_does_not_inherit_extra_headers(waf_config):
    headers = _headers("https://other.example.com/v1")
    assert headers.get("User-Agent") != "CloverAgent/1.0"
    assert "X-Privacy-Tier" not in headers


def test_extra_headers_apply_to_bearer_auth_branch(waf_config):
    # sk-ant-oat... is an OAuth token: different auth branch, same override rule.
    headers = _headers(_ROUTE, key="sk-ant-oat01-" + "x" * 40)
    assert headers["User-Agent"] == "CloverAgent/1.0"
    assert headers["X-Privacy-Tier"] == "enterprise"

"""Codex presence admission, offline: no OAuth refresh or physical requests.

The generic Codex auxiliary is NOT a bounded presence transport. These tests
require a precise hold before credential selection, even if a user explicitly
selects the same Codex model as their main conversation.
"""
import asyncio
from pathlib import Path

import pytest

from agent import auxiliary_client as aux


MODEL = "gpt-6-astra-900k"  # Synthetic configured route, not live availability.


def test_explicit_codex_presence_reports_missing_server_cap_before_credentials(monkeypatch):
    monkeypatch.setattr("clover_cli.config.load_config", lambda **kw: {
        "model": {"provider": "openai-codex", "default": MODEL},
        "auxiliary": {"presence": {"provider": "openai-codex", "model": MODEL}},
    })
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail("budget-blocked presence touched credentials or provider resolution")

    monkeypatch.setattr(aux, "resolve_provider_client", forbidden)
    monkeypatch.setattr(aux, "_select_pool_entry", forbidden)
    monkeypatch.setattr(aux, "_read_codex_access_token", forbidden)
    with pytest.raises(ValueError) as caught:
        asyncio.run(aux.presence_completion(
            messages=[{"role": "user", "content": "synthetic capsule"}],
            max_output_tokens=512, timeout=1,
        ))
    assert getattr(caught.value, "reason", None) == "codex_output_budget_unsupported"
    assert "server-enforced output cap" in str(caught.value)
    assert calls == []
    assert Path(aux.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("provider", ["codex-app-server", "bedrock"])
def test_unsupported_presence_routes_never_borrow_main_codex(monkeypatch, provider):
    monkeypatch.setattr("clover_cli.config.load_config", lambda **kw: {
        "model": {"provider": "openai-codex", "default": MODEL},
        "auxiliary": {"presence": {"provider": provider, "model": MODEL}},
    })

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported presence route entered generic provider resolution")

    monkeypatch.setattr(aux, "resolve_provider_client", forbidden)
    with pytest.raises(ValueError, match="unsupported bounded presence route"):
        asyncio.run(aux.presence_completion(
            messages=[{"role": "user", "content": "synthetic capsule"}],
            max_output_tokens=512, timeout=1,
        ))


@pytest.mark.parametrize("limit", [0, True, 513])
def test_invalid_reservations_do_not_become_codex_capability_errors(monkeypatch, limit):
    monkeypatch.setattr("clover_cli.config.load_config", lambda **kw: {
        "auxiliary": {"presence": {"provider": "openai-codex", "model": MODEL}},
    })
    with pytest.raises(ValueError, match="unsupported bounded presence route") as caught:
        asyncio.run(aux.presence_completion(
            messages=[{"role": "user", "content": "synthetic capsule"}],
            max_output_tokens=limit, timeout=1,
        ))
    assert not isinstance(caught.value, aux.PresenceOutputBudgetUnsupported)


def test_rotation_and_app_server_selection_cannot_bypass_budget_hold(monkeypatch):
    config = {
        "model": {"provider": "openai-codex", "default": MODEL,
                  "openai_runtime": "codex-app-server"},
        "auxiliary": {"presence": {"provider": "openai-codex", "model": MODEL}},
    }
    monkeypatch.setattr("clover_cli.config.load_config", lambda **kw: config)
    selected = []

    def synthetic_pool(*args, **kwargs):
        selected.append(True)
        pytest.fail("budget-blocked presence selected or rotated an account")

    monkeypatch.setattr(aux, "_select_pool_entry", synthetic_pool)
    for limit in (1, 512):
        with pytest.raises(aux.PresenceOutputBudgetUnsupported):
            asyncio.run(aux.presence_completion(
                messages=[{"role": "user", "content": "synthetic capsule"}],
                max_output_tokens=limit, timeout=1,
            ))
    assert selected == []
    assert config["model"]["default"] == MODEL
    assert config["model"]["openai_runtime"] == "codex-app-server"

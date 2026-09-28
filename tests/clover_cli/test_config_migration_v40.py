"""v40 config migration: the hosted provider and its managed tools were removed.

Old config in → expected config out, plus a second run that changes nothing.
"""

from __future__ import annotations

import copy
import json
import os
from unittest.mock import patch

import yaml

from clover_cli.config import migrate_config
from clover_cli.config_migrations import _v40_rewrite_config


OLD_CONFIG = {
    "_config_version": 39,
    "model": {
        "provider": "clover",
        "default": "anthropic/claude-opus-4.8",
        "base_url": "hosted-inference-endpoint",
        "api_mode": "anthropic_messages",
    },
    "fallback_model": {"provider": "clover", "model": "some/model"},
    "fallback_providers": [
        {"provider": "clover", "model": "a"},
        {"provider": "openrouter", "model": "b"},
        "clover",
    ],
    "auxiliary": {
        "vision": {"provider": "clover", "model": "v", "base_url": "hosted-aux-endpoint"},
        "compression": {"provider": "openrouter", "model": "c"},
    },
    "delegation": {"provider": "clover", "model": "d"},
    "web": {"backend": "clover", "search_backend": "clover", "extract_backend": "tavily"},
    "tts": {"provider": "clover", "voice": "alloy"},
    "stt": {"provider": "local", "use_gateway": False},
    "browser": {"cloud_provider": "clover"},
    "image_gen": {"use_gateway": True},
    "video_gen": {"provider": "clover", "use_gateway": True},
    "terminal": {"backend": "modal", "modal_mode": "managed"},
    "cron": {"provider": "chronos", "chronos": {"callback_url": ""}, "wrap_response": True},
    "dashboard": {"oauth": {"client_id": "x", "portal_url": "", "self_hosted": {"issuer": "i"}}},
    "display": {"credits_notices": True, "compact": False},
    "tool_gateway_declined_tools": ["web"],
    "sync": {"org_auto_propose": True},
    "memory": {"memory_enabled": True},
}

EXPECTED_CONFIG = {
    "model": {"provider": "auto", "default": "anthropic/claude-opus-4.8"},
    "fallback_providers": [{"provider": "openrouter", "model": "b"}],
    "auxiliary": {
        "vision": {"provider": "auto", "model": "v"},
        "compression": {"provider": "openrouter", "model": "c"},
    },
    "delegation": {"provider": "", "model": "d"},
    "web": {"backend": "firecrawl", "search_backend": "firecrawl", "extract_backend": "tavily"},
    "tts": {"provider": "openai", "voice": "alloy"},
    "stt": {"provider": "local"},
    "browser": {"cloud_provider": "browser-use"},
    "image_gen": {"provider": "fal"},
    "video_gen": {"provider": "fal"},
    "terminal": {"backend": "modal", "modal_mode": "direct"},
    "cron": {"provider": "", "wrap_response": True},
    "dashboard": {"oauth": {"self_hosted": {"issuer": "i"}}},
    "display": {"compact": False},
    "memory": {"memory_enabled": True},
}


def _write(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_rewrite_maps_old_config_to_expected():
    config = copy.deepcopy(OLD_CONFIG)
    config.pop("_config_version")

    notes = _v40_rewrite_config(config)

    assert config == EXPECTED_CONFIG
    assert "model.provider → auto" in notes


def test_rewrite_second_pass_changes_nothing():
    config = copy.deepcopy(OLD_CONFIG)
    _v40_rewrite_config(config)
    once = copy.deepcopy(config)

    assert _v40_rewrite_config(config) == []
    assert config == once


def test_rewrite_leaves_unrelated_config_alone():
    config = {"model": {"provider": "openrouter", "default": "x/y"}, "web": {"backend": "exa"}}
    before = copy.deepcopy(config)

    assert _v40_rewrite_config(config) == []
    assert config == before


def test_migrate_config_v39_to_v40_end_to_end(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOVER_SHARED_AUTH_DIR", raising=False)
    shared_dir = tmp_path / "shared-auth"
    shared_dir.mkdir()
    (shared_dir / "clover_auth.json").write_text("{}", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    _write(config_path, OLD_CONFIG)
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": "clover",
                "providers": {"clover": {"access_token": "t"}, "openai-codex": {"tokens": {}}},
                "credential_pool": {"clover": [{"id": "1"}], "openrouter": [{"id": "2"}]},
            }
        ),
        encoding="utf-8",
    )

    env = {"CLOVER_HOME": str(tmp_path), "CLOVER_SHARED_AUTH_DIR": str(shared_dir)}
    with patch.dict(os.environ, env):
        results = migrate_config(interactive=False, quiet=True)

    raw = _read(config_path)
    assert raw.pop("_config_version") >= 40
    # Earlier ladder steps may add their own keys; check ours field by field.
    for key, value in EXPECTED_CONFIG.items():
        assert raw[key] == value, key
    for removed in ("fallback_model", "tool_gateway_declined_tools", "sync"):
        assert removed not in raw
    assert "model.provider → auto" in results["config_added"]

    store = json.loads(auth_path.read_text(encoding="utf-8"))
    assert "active_provider" not in store
    assert "clover" not in store["providers"]
    assert "openai-codex" in store["providers"]
    assert "clover" not in store["credential_pool"]
    assert store["credential_pool"]["openrouter"] == [{"id": "2"}]
    assert not (shared_dir / "clover_auth.json").exists()

    # Second run: nothing left to change.
    migrated_config = config_path.read_text(encoding="utf-8")
    migrated_auth = auth_path.read_text(encoding="utf-8")
    with patch.dict(os.environ, env):
        second = migrate_config(interactive=False, quiet=True)

    assert config_path.read_text(encoding="utf-8") == migrated_config
    assert auth_path.read_text(encoding="utf-8") == migrated_auth
    assert "model.provider → auto" not in second["config_added"]


def test_saved_clover_provider_resolves_to_auto_after_migration(tmp_path):
    config_path = tmp_path / "config.yaml"
    _write(config_path, {"_config_version": 39, "model": {"provider": "clover", "default": "m"}})

    with patch.dict(os.environ, {"CLOVER_HOME": str(tmp_path)}):
        migrate_config(interactive=False, quiet=True)
        from clover_cli import runtime_provider as rp

        with patch.object(rp, "_get_model_config", lambda: _read(config_path)["model"]):
            assert rp.resolve_requested_provider() == "auto"

    assert _read(config_path)["model"]["provider"] == "auto"

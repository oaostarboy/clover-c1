"""Telegram gating keys in config.yaml must survive the loader bridge.

`load_gateway_config` bridges `platforms.telegram.*` into `PlatformConfig.extra`
via an EXPLICIT per-key allowlist. A key the adapter reads but the bridge does
not name is silently dropped: config looks correct on disk, the adapter falls
back to its env/default, and the operator's setting does nothing.

That failure is invisible -- no error, no warning, no log line. It shipped a
security setting (`read_only_chats`, an unconditional mute) that never took
effect, so the bot kept replying in a room it was configured to stay silent in.

These tests assert the CONTRACT -- every gating key the adapter reads is
bridged -- rather than snapshotting today's key list, so a newly added adapter
key fails here instead of failing silently in production.
"""

import inspect
import os
import re

import pytest

from gateway.config import Platform, load_gateway_config


@pytest.fixture(autouse=True)
def _isolate_telegram_env():
    """Undo the loader's YAML-to-env bridge after every test in this module.

    ``load_gateway_config`` bridges ``platforms.telegram.*`` into process env
    vars (first-writer-wins). Those writes are global and outlive the test, so
    without this fixture the parametrized cases below leave
    ``TELEGRAM_ALLOWED_CHATS=sentinel`` (and friends) behind and silently break
    every later test that expects an empty allowlist -- exactly the kind of
    cross-test coupling this file exists to catch.

    Restores ``os.environ`` directly rather than via ``monkeypatch``: a
    monkeypatch-based teardown here would itself be undone by monkeypatch's
    own teardown, which runs afterwards and would put the leaked values back.
    """
    preserved = {k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_")}
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith("TELEGRAM_")]:
            del os.environ[key]
        os.environ.update(preserved)


# Adapter accessors that read a gating key out of config.extra, mapped to the
# key each one reads. Adding an accessor without a bridge line breaks silently
# in production; this map is what makes it break loudly in CI instead.
GATING_KEYS = [
    "allowed_chats",
    "group_allowed_chats",
    "allowed_topics",
    "read_only_chats",
    "read_only_except_from",
    "group_default",
    "free_response_chats",
    "free_response_topics",
    "ignored_threads",
    "guest_mode",
    "require_mention",
    "observe_unmentioned_group_messages",
    "exclusive_bot_mentions",
    "mention_patterns",
]


def _write_config(tmp_path, monkeypatch, telegram_section: str):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-opus-5\n"
        "platforms:\n"
        "  telegram:\n" + telegram_section
    )
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    return cfg


def _telegram_extra(tmp_path, monkeypatch, telegram_section):
    _write_config(tmp_path, monkeypatch, telegram_section)
    cfg = load_gateway_config()
    platform = cfg.platforms.get(Platform.TELEGRAM)
    assert platform is not None, "telegram platform did not load at all"
    return platform.extra


def test_read_only_chats_survives_the_bridge(tmp_path, monkeypatch):
    """The regression: a mute configured on disk that never reached the adapter."""
    extra = _telegram_extra(
        tmp_path,
        monkeypatch,
        '    read_only_chats:\n      - "-5495436820"\n',
    )
    assert extra.get("read_only_chats") == ["-5495436820"], (
        "read_only_chats was dropped by the config bridge -- the bot would "
        "keep replying in a chat configured to be silent"
    )


def test_read_only_except_from_survives_the_bridge(tmp_path, monkeypatch):
    extra = _telegram_extra(
        tmp_path,
        monkeypatch,
        '    read_only_except_from:\n      - "8172525590"\n',
    )
    assert extra.get("read_only_except_from") == ["8172525590"]


@pytest.mark.parametrize("key", GATING_KEYS)
def test_every_gating_key_is_bridged(key, tmp_path, monkeypatch):
    """Contract: a gating key set in config.yaml must arrive in extra.

    Uses a list value for every key because the bridge copies values verbatim;
    what is under test is presence, not type coercion.
    """
    extra = _telegram_extra(tmp_path, monkeypatch, f'    {key}:\n      - "sentinel"\n')
    assert key in extra, (
        f"'{key}' is read by the telegram adapter but the config bridge in "
        f"gateway/config.py does not copy it into PlatformConfig.extra. "
        f"It will be silently ignored at runtime."
    )
    assert extra[key] == ["sentinel"]


def test_adapter_gating_accessors_all_have_bridge_lines():
    """Catch the NEXT dropped key, not just the ones already fixed.

    Scans the adapter for `config.extra.get("...")` reads and the loader for
    bridge lines, and fails when an adapter key has no bridge. This is what
    turns a silent production failure into a CI failure.
    """
    from plugins.platforms.telegram import adapter as tg_adapter
    from gateway import config as gw_config

    adapter_src = inspect.getsource(tg_adapter)
    loader_src = inspect.getsource(gw_config)

    adapter_keys = set(re.findall(r'extra\.get\(\s*["\']([a-z_]+)["\']', adapter_src))
    bridged_keys = set(re.findall(r'bridged\[["\']([a-z_]+)["\']\]', loader_src))

    # Keys that are legitimately adapter-local (connection/display concerns
    # configured elsewhere or read straight from env), not group gating.
    non_gating = {
        "base_url",
        "base_file_url",
        "local_mode",
        "fallback_ips",
        "disable_link_previews",
        "status_indicator",
        "status_online",
        "status_offline",
        "dm_topics",
        "group_topics",
        "ignore_root_dm",
        "reply_to_mode",
        "group_sessions_per_user",
        "thread_sessions_per_user",
        "unauthorized_dm_behavior",
        "ingest_unmentioned_group_messages",  # legacy alias of the observe key
        "allow_from",
        "group_allow_from",
    }

    missing = (adapter_keys - bridged_keys) - non_gating
    assert not missing, (
        "These telegram config keys are read by the adapter but never bridged "
        f"by load_gateway_config, so setting them in config.yaml does nothing: "
        f"{sorted(missing)}. Add a bridge line in gateway/config.py, or add the "
        "key to `non_gating` if it is genuinely not a gating setting."
    )

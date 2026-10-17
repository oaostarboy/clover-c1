"""Exercise shared gateway dispatch, explicit admin boundary and safe fallback."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from agent.workstreams import load_settings
from gateway.config import Platform
from gateway.run import GatewayRunner
from tests.gateway.test_slash_access_dispatch import _make_runner, _make_event, _make_source


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    root.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(root))
    return root


def runner():
    return _make_runner(platform=Platform.TELEGRAM, platform_extra={
        "allow_admin_from": ["owner"], "user_allowed_commands": ["proactive"],
        "group_user_allowed_commands": ["proactive"],
    })


def event(text, uid="owner", **kwargs):
    return _make_event(text, _make_source(platform=Platform.TELEGRAM, user_id=uid, **kwargs))


@pytest.mark.asyncio
async def test_real_gateway_dispatch_and_no_llm(home):
    r = runner()
    r._handle_message_with_agent = AsyncMock(side_effect=AssertionError("no model"))
    text = await r._handle_message(event("/proactive"))
    assert "Off" in text and "/proactive on" in text
    assert not (home / "config.yaml").exists()
    text = await r._handle_message(event("/proactive on"))
    assert "Saved" in text and load_settings().active
    assert "On" in await r._handle_message(event("/proactive status"))
    assert "Off" in await r._handle_message(event("/proactive off"))
    assert not load_settings().enabled
    r._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("uid,ctype", [("guest", "dm"), ("owner", "group"), ("guest", "group")])
async def test_untrusted_users_and_non_admin_group_cannot_write(home, uid, ctype):
    r = runner()
    text = await r._handle_message(event("/proactive on", uid, chat_type=ctype))
    assert "admin" in text.lower()
    assert not (home / "config.yaml").exists()


@pytest.mark.asyncio
async def test_no_admin_list_is_not_admin_authority(home):
    r = _make_runner(platform=Platform.TELEGRAM)
    text = await r._handle_message(event("/proactive on"))
    assert "admin" in text.lower()
    assert not load_settings().enabled


@pytest.mark.asyncio
async def test_legacy_syntax_does_not_create_loop_or_toggle(home):
    r = runner()
    r._handle_loop_command = AsyncMock(side_effect=AssertionError("legacy loop executed"))
    text = await r._handle_message(event("/proactive 5m check CI"))
    assert "/loop <interval> <prompt>" in text
    assert not load_settings().enabled
    r._handle_loop_command.assert_not_called()


@pytest.mark.asyncio
async def test_internal_and_stale_commands_cannot_toggle(home):
    r = runner()
    ev = event("/proactive on")
    ev.internal = True
    assert "current" in (await r._handle_proactive_command(ev)).lower()
    ev.internal = False
    r._startup_time = datetime.now().timestamp()
    ev.timestamp = datetime.now() - timedelta(hours=1)
    assert "old" in (await r._handle_proactive_command(ev)).lower()
    assert not load_settings().enabled


@pytest.mark.asyncio
async def test_profile_wrapper_persists_only_target_and_uses_its_admin_policy(home, tmp_path, monkeypatch):
    from pathlib import Path
    import yaml
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from clover_cli.profiles import get_profile_dir
    target = get_profile_dir("menu-test")
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(yaml.safe_dump({
        "platforms": {"telegram": {"enabled": True, "allow_admin_from": ["owner"]}}
    }))
    r = runner()
    r.config.multiplex_profiles = True
    handler = r._make_profile_message_handler("menu-test")
    text = await handler(event("/proactive on"))
    assert "Saved" in text
    assert yaml.safe_load((target / "config.yaml").read_text())["proactive"]["enabled"] is True
    assert not (home / "config.yaml").exists()
    assert not load_settings().enabled
    # A transport admin is not automatically an admin of a different profile.
    other = get_profile_dir("unowned")
    other.mkdir()
    (other / "config.yaml").write_text("{}")
    text = await r._make_profile_message_handler("unowned")(event("/proactive on"))
    assert "admin" in text.lower()
    assert "proactive" not in yaml.safe_load((other / "config.yaml").read_text())


@pytest.mark.asyncio
async def test_busy_control_path_is_deterministic_and_does_not_interrupt(home):
    from clover_cli.commands import resolve_command
    r = runner()
    ev = event("/proactive on")
    text = await r._dispatch_busy_slash_command(ev, resolve_command("proactive"), "session", ev.source)
    assert "Saved" in text and load_settings().active


@pytest.mark.asyncio
async def test_non_slash_adapters_show_usable_text_controls(home):
    class TypedAdapter:
        typed_command_prefix = "!"
    r = runner()
    r.adapters[Platform.TELEGRAM] = TypedAdapter()
    text = await r._handle_message(event("/proactive"))
    assert "!proactive on" in text and "!proactive off" in text
    text = await r._handle_message(event("/proactive 5m check"))
    assert "!loop <interval> <prompt>" in text

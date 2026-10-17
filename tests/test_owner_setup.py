"""Setup + CLI parser + actual Telegram ingress, offline endpoints only."""
import argparse
import asyncio
import re
from unittest.mock import Mock

import pytest
from telegram.ext import Application

from clover_cli import config
from clover_cli import pairing as cli
from clover_cli import setup
from clover_cli.subcommands.pairing import build_pairing_parser
from gateway.config import PlatformConfig
from gateway.pairing import VerifiedOwnerStore
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.test_owner_telegram_wire import TOKEN, TelegramEndpoint, update


def args(action):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    build_pairing_parser(sub, cmd_pairing=cli.pairing_command)
    return parser.parse_args(["pairing", action, "telegram"])


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    config.save_config({"proactive": {"enabled": False}, "platforms": {"telegram": {"enabled": True, "allow_from": ["12345678"]}}}, strip_defaults=False)
    config.load_config()
    return tmp_path


def bot_proof(challenge):
    async def run():
        endpoint = TelegramEndpoint()
        app = Application.builder().token(TOKEN).request(endpoint).build()
        a = TelegramAdapter(PlatformConfig(enabled=True, token=TOKEN))
        a._bot = app.bot
        a._register_handlers(app)
        async with app:
            await app.process_update(update(app, "/start cloverowner_" + challenge))
        text = [p["text"] for m, p in endpoint.calls if m == "sendMessage"][0]
        return text.split("Confirmation code: ", 1)[1].split()[0]
    return asyncio.run(run())


def test_cli_wizard_automatically_saves_only_after_actual_sender_and_local_confirmation(profile, monkeypatch, capsys):
    before = (profile / "config.yaml").read_bytes()
    monkeypatch.setattr(setup, "prompt_yes_no", lambda *a, **k: True)

    def confirm(question, **kwargs):
        assert kwargs.get("password") is True, "return code must not echo or enter argv/history"
        output = capsys.readouterr().out
        challenge = re.search(r"/start cloverowner_([A-Za-z0-9_-]{32})", output).group(1)
        return bot_proof(challenge)

    monkeypatch.setattr(setup, "prompt", confirm)
    parsed = args("owner-setup")
    parsed.func(parsed)
    output = capsys.readouterr().out
    assert "verified and saved" in output.lower()
    assert "12345678" in output
    assert VerifiedOwnerStore(TOKEN).is_owner("12345678")
    assert (profile / "config.yaml").read_bytes() == before
    state = VerifiedOwnerStore(TOKEN).path.read_bytes()
    monkeypatch.setattr(setup, "prompt", Mock(side_effect=AssertionError("must reuse verified proof")))
    parsed.func(parsed)
    assert "already verified" in capsys.readouterr().out.lower()
    assert VerifiedOwnerStore(TOKEN).path.read_bytes() == state
    revoke = args("owner-revoke")
    revoke.func(revoke)
    assert not VerifiedOwnerStore(TOKEN).is_owner("12345678")


def test_missing_proof_legacy_setup_can_defer_and_finish_once(profile, monkeypatch, capsys):
    before = (profile / "config.yaml").read_bytes()
    monkeypatch.setattr(setup, "prompt", lambda *a, **k: "")
    parsed = args("owner-setup")
    parsed.func(parsed)
    output = capsys.readouterr().out
    challenge = re.search(r"/start cloverowner_([A-Za-z0-9_-]{32})", output).group(1)
    assert "not verified" in output.lower()
    assert not VerifiedOwnerStore(TOKEN).is_owner("12345678")
    code = bot_proof(challenge)
    monkeypatch.setattr(setup, "prompt", lambda *a, **k: code)
    finish = args("owner-confirm")
    finish.func(finish)
    assert "verified and saved" in capsys.readouterr().out.lower()
    finish.func(finish)
    assert "not verified" in capsys.readouterr().out.lower()  # Consumed return code cannot re-enroll.
    assert (profile / "config.yaml").read_bytes() == before


def test_real_setup_offers_owner_ceremony_after_gateway_connection(profile, monkeypatch, capsys):
    from clover_cli import gateway
    phases = []
    monkeypatch.setattr(gateway, "_all_platforms", lambda: [{"key": "telegram", "emoji": "T", "label": "Telegram"}])
    monkeypatch.setattr(gateway, "_platform_status", lambda p: "configured")
    monkeypatch.setattr(gateway, "_is_service_running", lambda: False)
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway, "ensure_gateway_service", lambda **k: phases.append("connected"))
    monkeypatch.setattr(setup, "prompt_checklist", lambda *a: [])
    monkeypatch.setattr(setup, "prompt_yes_no", lambda *a, **k: True)

    def confirm(question, **kwargs):
        assert phases == ["connected"]
        output = capsys.readouterr().out
        challenge = re.search(r"/start cloverowner_([A-Za-z0-9_-]{32})", output).group(1)
        return bot_proof(challenge)

    monkeypatch.setattr(setup, "prompt", confirm)
    setup.setup_gateway(config.load_config())
    assert VerifiedOwnerStore(TOKEN).is_owner("12345678"), "fresh setup must offer actual sender verification"


def test_cli_persistence_failure_has_no_false_success(profile, monkeypatch, capsys):
    s = VerifiedOwnerStore(TOKEN)
    code = s.prove(s.begin(), "12345678")
    monkeypatch.setattr(setup, "prompt", lambda *a, **k: code)
    monkeypatch.setattr("gateway.pairing._secure_write", lambda *a: None)
    parsed = args("owner-confirm")
    parsed.func(parsed)
    output = capsys.readouterr().out.lower()
    assert "verified and saved" not in output
    assert "not verified" in output
    assert not s.is_owner("12345678")


def test_gateway_setup_also_offers_the_same_owner_verification(profile, monkeypatch):
    from clover_cli import gateway
    monkeypatch.setattr(gateway, "is_managed", lambda: False)
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway, "_is_service_installed", lambda: True)
    monkeypatch.setattr(gateway, "_is_service_running", lambda: True)
    monkeypatch.setattr(gateway, "_all_platforms", lambda: [{"key": "telegram", "emoji": "T", "label": "Telegram"}])
    monkeypatch.setattr(gateway, "_platform_status", lambda p: "configured")
    monkeypatch.setattr(gateway, "prompt_choice", lambda *a: 1)  # Done, no platform edits.
    monkeypatch.setattr(gateway, "prompt_yes_no", lambda *a, **k: False)  # Do not change service state.
    offer = Mock(return_value=False)
    monkeypatch.setattr(cli, "setup_verified_telegram_owner", offer)
    gateway.gateway_setup()
    offer.assert_called_once_with()


def test_explicit_admin_grants_are_reused_without_silent_migration(profile, monkeypatch, capsys):
    config.set_config_value("platforms.telegram.allow_admin_from", '["87654321"]')
    before = (profile / "config.yaml").read_bytes()
    monkeypatch.setattr(setup, "prompt_yes_no", Mock(side_effect=AssertionError("existing admin policy is authoritative")))
    assert cli.setup_verified_telegram_owner()
    assert "explicit" in capsys.readouterr().out.lower()
    assert not VerifiedOwnerStore(TOKEN).path.exists()
    assert (profile / "config.yaml").read_bytes() == before


def test_setup_decline_has_no_ownership_or_config_side_effects(profile, monkeypatch):
    before = (profile / "config.yaml").read_bytes()
    monkeypatch.setattr(setup, "prompt_yes_no", lambda *a, **k: False)
    assert not cli.setup_verified_telegram_owner()
    assert not VerifiedOwnerStore(TOKEN).path.exists()
    assert (profile / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("managed_yaml", [
    "platforms:\n  telegram:\n    verified_owner_controls: false\n",
    "platforms:\n  telegram:\n    extra:\n      verified_owner_controls: false\n",
])
def test_setup_does_not_offer_a_grant_over_managed_denial(profile, monkeypatch, tmp_path, capsys, managed_yaml):
    from clover_cli import managed_scope
    config.set_config_value("platforms.telegram.verified_owner_controls", "true")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(managed_yaml)
    monkeypatch.setenv("CLOVER_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    monkeypatch.setattr(setup, "prompt_yes_no", Mock(side_effect=AssertionError("managed denial must suppress ceremony")))
    try:
        assert not cli.setup_verified_telegram_owner()
        assert "disabled" in capsys.readouterr().out.lower()
        assert not VerifiedOwnerStore(TOKEN).path.exists()
    finally:
        managed_scope.invalidate_managed_cache()


def test_real_cli_process_loads_exact_profile_and_confirms_saved_owner(profile, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    s = VerifiedOwnerStore(TOKEN)
    code = s.prove(s.begin(), "12345678")
    child_home = tmp_path / "child-home"
    child_home.mkdir()
    # Native Windows Path.home() uses USERPROFILE, not HOME. Supply only
    # disposable native locations; never borrow the runner's user profile.
    local_appdata = child_home / "AppData" / "Local"
    appdata = child_home / "AppData" / "Roaming"
    local_appdata.mkdir(parents=True)
    appdata.mkdir(parents=True)
    env = {"HOME": str(child_home), "USERPROFILE": str(child_home),
           "LOCALAPPDATA": str(local_appdata), "APPDATA": str(appdata),
           "CLOVER_HOME": str(profile), "TELEGRAM_BOT_TOKEN": TOKEN,
           "PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    # Required on native Windows to locate core DLLs; no credential borrowing.
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if name in os.environ: env[name] = os.environ[name]
    command = [sys.executable, "-m", "clover_cli.main", "pairing", "owner-confirm", "telegram"]
    if os.name == "nt":
        # This is a piped-input/bootstrap test, not a native console test.
        # Windows getpass otherwise reads the console even when fd 0 is a pipe.
        # A real redirected Python stream selects its stdlib fallback; keep
        # the proof on stdin and run the complete, unmocked CLI module.
        command = [sys.executable, "-c",
                   "import io, runpy, sys; "
                   "sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding=sys.stdin.encoding, errors=sys.stdin.errors); "
                   "sys.argv = ['clover_cli.main', 'pairing', 'owner-confirm', 'telegram']; "
                   "runpy.run_module('clover_cli.main', run_name='__main__')"]
    result = subprocess.run(command,
                            input=code + "\n", text=True, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "verified and saved" in result.stdout.lower(), result.stdout + result.stderr
    assert code not in result.stdout + result.stderr
    assert VerifiedOwnerStore(TOKEN, home=profile).is_owner("12345678")
    assert not (child_home / ".clover" / "platforms" / "telegram-owner.json").exists()
    assert not (local_appdata / "clover" / "platforms" / "telegram-owner.json").exists()
